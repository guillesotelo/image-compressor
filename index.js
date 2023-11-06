const fs = require('fs')
const path = require('path')
const sharp = require('sharp')

if (!process.argv[2] || !process.argv[3]) return console.log('Please enter source and destination folder paths\nUse the syntax: node index /source/path /destination/path')
if (process.argv[4] && process.argv[4] < 1 || process.argv[4] < 100) return console.log('Please enter a quality between 1 and 100')

const sourceFolder = process.argv[2]
const destinationFolder = process.argv[3]
const globalQuality = process.argv[4] || 80
const maxWidth = 3000
const maxHeight = 1500
const maxSize = 500000
let compressedFiles = 0

console.log('process.argv[1]', process.argv[1])
console.log('process.argv[2]', process.argv[2])

sharp.cache(false) // Disable caching to ensure the global quality setting takes effect
sharp.concurrency(1) // Set concurrency to 1 for consistent quality across multiple operations

sharp.queue.on('task', (info) => {
    if (info.format === 'jpeg'
        || info.format === 'png'
        || info.format === 'webp'
        || info.format === 'gif') {
        info.options.quality = globalQuality
    }
})

// Function to process and compress images with size and dimension limits
async function processAndCompressImage(inputPath, outputPath) {
    try {
        await sharp(inputPath)
            .resize({
                width: maxWidth,
                height: maxHeight,
                fit: 'inside',
                withoutEnlargement: true
            })
            .toBuffer({ resolveWithObject: true, size: maxSize })
            .then(({ data, info }) => {
                // Check if the image is within the size limit
                fs.writeFileSync(outputPath, data)
                console.log(`Compressed: ${inputPath}`)
            });
    } catch (err) {
        console.error(`Error compressing: ${inputPath}`, err);
    }
}


// Function to read and process files
function processFilesInFolder(folderPath) {
    try {
        const files = fs.readdirSync(folderPath)

        files.forEach((file) => {
            const filePath = path.join(folderPath, file)
            const stats = fs.statSync(filePath)

            if (stats.isDirectory()) {
                // If it's a directory, recursively process its contents
                const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                fs.mkdirSync(newDestinationFolder, { recursive: true })
                processFilesInFolder(filePath)
            } else {
                // If it's a file, you can check if it's an image and compress it
                if (['.jpg', '.jpeg', '.png', '.gif'].includes(path.extname(file).toLowerCase())) {
                    // Create the corresponding subfolder structure in the destination folder
                    const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                    fs.mkdirSync(newDestinationFolder, { recursive: true })

                    // Construct the destination file path
                    const destinationFilePath = path.join(newDestinationFolder, file)

                    // Process and compress the image
                    processAndCompressImage(filePath, destinationFilePath)
                } else {
                    // If it's not an image, simply copy it to the destination folder
                    const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                    fs.mkdirSync(newDestinationFolder, { recursive: true })
                    fs.copyFileSync(filePath, path.join(newDestinationFolder, file))
                }
            }
        })
    } catch (err) {
        console.log('An error occurred while running the conversor', err)
    }
}

// Start processing files from the source folder
processFilesInFolder(sourceFolder)
console.log(`Compressed ${compressedFiles} files.`)
