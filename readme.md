# Image and Video Compressor

This script converts image and video files (jpg, jpeg, png, gif, mp4, avi and mkv) and compresses them using *sharp* and *ffmpeg* algorithms.
The entire folder structure is maintained in the destination folder.
For images, percentage of compression is by default at 80%, but it can be overwritten passing the percentage as a third argument.
For videos, the video bitrate is 500k and audio is 128k.

## Run conversion

Start the conversion process by running: 

```bash
node index path_to_source path_to_destination
```

If you want to use a specific percentage of compression for images, pass it as a third argument:

```bash
# Example with 60% of compression
node index path_to_source path_to_destination 60
```

To compress videos, use the flag `-v`:

```bash
node index path_to_source path_to_destination --v
```