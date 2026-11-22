# LightestVideoPlayer Server

Local HLS relay server for LightestVideoPlayer.

## Requirements

- Python
- yt-dlp

## Usage

```bash
python lvp_server.py
```


```bash
python lvp_server.py --port 8765
```

## Auto-Start on Windows

1. Open `start_lvp_server.vbs` and replace `C:\path\lvp_server.py` with the actual path to `lvp_server.py`.
2. Press `Win + R`, enter `shell:startup`, and press Enter.
3. Place `start_lvp_server.vbs` in the opened Startup folder.
