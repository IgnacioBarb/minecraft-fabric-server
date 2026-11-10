"""Cliente RCON de Minecraft (protocolo Source RCON), con una conexión que se reutiliza."""

import re
import socket
import struct
import threading

# Códigos de color de la terminal y de Minecraft (§a, §l…) que a veces vienen en las respuestas.
_CODES = re.compile(r"\x1b\[[0-9;]*m|§.")

AUTH, EXEC, RESPONSE = 3, 2, 0


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host, port, password):
        self.host, self.port, self.password = host, port, password
        self.sock = None
        self.lock = threading.Lock()
        self._id = 0

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _next_id(self):
        self._id = self._id % 2_000_000_000 + 1
        return self._id

    def _send(self, req_id, kind, body):
        data = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(data)) + data)

    def _recv_exact(self, n):
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("el servidor cerró la conexión RCON")
            buf += chunk
        return bytes(buf)

    def _read(self):
        (length,) = struct.unpack("<i", self._recv_exact(4))
        if not 10 <= length <= 1 << 20:
            raise RconError("paquete RCON inválido")
        data = self._recv_exact(length)
        req_id, kind = struct.unpack("<ii", data[:8])
        return req_id, kind, data[8:-2].decode("utf-8", "replace")

    def _connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=5)
        self._send(self._next_id(), AUTH, self.password)
        req_id, _, _ = self._read()
        if req_id == -1:
            self.close()
            raise RconError("contraseña RCON incorrecta")

    def command(self, cmd, timeout=10):
        """Ejecuta un comando y devuelve la respuesta como texto (sin códigos de color)."""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    self.sock.settimeout(timeout)
                    cmd_id, end_id = self._next_id(), self._next_id()
                    self._send(cmd_id, EXEC, cmd)
                    # Las respuestas largas llegan en varios paquetes. Minecraft contesta a un paquete de
                    # tipo desconocido con "Unknown request", y en orden: así se sabe dónde acaba la respuesta.
                    self._send(end_id, RESPONSE, "")
                    parts = []
                    while True:
                        req_id, _, body = self._read()
                        if req_id == cmd_id:
                            parts.append(body)
                        elif req_id == end_id:
                            break
                    return _CODES.sub("", "".join(parts))
                except (OSError, RconError, struct.error) as e:
                    self.close()
                    if attempt == 2:
                        raise RconError(str(e) or e.__class__.__name__) from e
