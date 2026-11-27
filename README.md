# LightestVideoPlayer Server

A local HLS relay server for LightestVideoPlayer.

Memory usage remains at 10 MB even while streaming.

It returns the YouTube HLS master m3u8 URL. Even if the video and audio are separated, it processes the EXT-X-MEDIA header from the hls.js master as-is.


## Requirements

- Python

https://www.python.org/downloads/
- yt-dlp
```bash
pip install -U yt-dlp
```

## Install

Requires Python 3.10+ and Git.

```bash
pip install git+https://github.com/asdfghjkl636625/LightestVideoPlayer-server.git
```

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
