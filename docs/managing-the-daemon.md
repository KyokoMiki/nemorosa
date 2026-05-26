# Managing the Daemon

Nemorosa can run as a daemon with HTTP API support, scheduled jobs, and webhook integration. If you use Docker, you can run it continuously through docker compose. If you install it locally, you need to install nemorosa as a service through systemd.

## Docker

You can use Docker Compose. Create or open your existing docker-compose.yml file and add the nemorosa service:

```yaml
services:
  nemorosa:
    image: ghcr.io/kyokomiki/nemorosa:latest
    container_name: nemorosa
    # user: "1000:1000"
    environment:
      - TZ=UTC  # Optional: Set timezone
    volumes:
      - /path/to/your/config:/app/data
      - /path/to/your/torrents:/app/torrents  # Map your torrent client's torrents directory
    ports:
      - "8256:8256"
    command: "--server"
```

After that, you can use the following commands to control it:

```bash
docker-compose pull    # Update the container to the latest version of nemorosa
docker-compose up -d   # Create/start the container
docker start nemorosa  # Start the daemon
docker stop nemorosa   # Stop the daemon
docker restart nemorosa # Restart the daemon
docker logs nemorosa   # View the logs
```

## systemd

If you want to use systemd to run nemorosa daemon continuously, you can create a unit file in `/etc/systemd/system`.

```bash
touch /etc/systemd/system/nemorosa.service
```

Open the file in your favorite editor, and paste the following code in:

```ini
[Unit]
Description=nemorosa server
[Service]
User=MyUserHere
Group=MyGroupHere
Restart=always
Type=simple
ExecStart=nemorosa --server
[Install]
WantedBy=multi-user.target
```

**Info:** Depending on how you installed nemorosa, you may need to specify absolute paths to nemorosa, using `/path/to/nemorosa --config CONFIG --server`

After installing the unit file, you can use these commands to control the daemon:

```bash
sudo systemctl daemon-reload  # Tell systemd to reindex to discover the unit file you just created
sudo systemctl enable nemorosa # Enable it to run on restart
sudo systemctl start nemorosa  # Start the service
sudo systemctl stop nemorosa   # Stop the service
sudo systemctl restart nemorosa # Restart the service
sudo journalctl -u nemorosa    # View the logs
```