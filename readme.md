# Image and Video Compressor

This script converts image and video files (jpg, jpeg, png, gif, mp4, avi and mkv) and compresses them using *sharp* and *ffmpeg* algorithms.
The entire folder structure is maintained in the destination folder.
For images, percentage of compression is by default at 80%, but it can be overwritten passing the percentage as a third argument.
For videos, the video bitrate is 500k and audio is 128k.

## Run conversion

### Python engine

Start the conversion process by running: 

```bash
python3 run_compression_v2.py path/to/source --video-quality 26 --photo-quality 85
```

If a re-encode ends up **bigger** than the source (common with photos that are
already compressed JPEGs), the original is copied over instead and the file is
reported as `kept original`. Pass `--allow-larger` to keep the bigger re-encode
anyway.

### JavaScript engine

Start the conversion process by running: 

```bash
node run_compression_v1.js path/to/source path/to/destination
```

If you want to use a specific percentage of compression for images, pass it as a third argument:

```bash
# Example with 60% of compression
node run_compression_v1.js path/to/source path/to/destination 60
```

To compress videos, use the flag `-v`:

```bash
node run_compression_v1.js path/to/source path/to/destination 80 --v
```