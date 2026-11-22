# LightestVideoPlayer Server

Local HLS relay server for LightestVideoPlayer.

It launches yt-dlp as a subprocess only when opening a video (using about 10 MB while idle),
and returns the YouTube HLS master m3u8 URL. Even if the video and audio are separated,
hls.js processes the master’s EXT-X-MEDIA as-is.

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
