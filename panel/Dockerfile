# The itzg image downloads Minecraft, Fabric and the Modrinth mods; the panel launches it with /start.
FROM itzg/minecraft-server:java25

RUN apt-get update && apt-get install -y --no-install-recommends python3-flask python3-waitress \
    && rm -rf /var/lib/apt/lists/*

COPY . /panel
WORKDIR /panel
# The image's "minecraft" user has UID 1000, usually the same as your user on the host.
USER 1000:1000
ENV PYTHONUNBUFFERED=1 HOME=/data
EXPOSE 8000 25565
ENTRYPOINT ["python3", "/panel/app.py"]
