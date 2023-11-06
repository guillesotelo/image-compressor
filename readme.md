# Image Compressor

This script converts image files (jpg, jpeg, png and gif) and compress them using sharp algorithm.
The entire folder structure is maintained in the destination folder.
The percentage of compression is by default at 80%, but it can ve overwritten passing the porcentage as a third argument.

## Run conversion

Start the conversion process by running: 

```bash
node index path_to_source path_to_destination
```

If you want to use a specific percentage of compression, pass it as parameter:

```bash
# Example with 60% of compression
node index path_to_source path_to_destination 60
```