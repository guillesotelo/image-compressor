const fs = require('fs')
const path = require('path')
const sharp = require('sharp')

const sourceFolder = process.argv[1] 
const destinationFolder = process.argv[2] 
const globalQuality = process.argv[3] || 80 

sharp.cache(false) // Disable caching to ensure the global quality setting takes effect
sharp.concurrency(1) // Set concurrency to 1 for consistent quality across multiple operations

sharp.queue.on('task', (info) => {
    if (info.format === 'jpeg' || info.format === 'png' || info.format === 'webp') {
        info.options.quality = globalQuality
    }
})

// Function to process and compress images
async function processAndCompressImage(inputPath, outputPath) {
    try {
        await sharp(inputPath).toFile(outputPath)
        console.log(`Compressed: ${inputPath}`)
    } catch (err) {
        console.error(`Error compressing: ${inputPath}`, err)
    }
}

// Function to read and process files
function processFilesInFolder(folderPath) {
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
}

// Start processing files from the source folder
processFilesInFolder(sourceFolder)
