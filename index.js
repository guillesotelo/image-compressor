const fs = require('fs')
const path = require('path')
const sharp = require('sharp')
const ffmpegPath = require('@ffmpeg-installer/ffmpeg').path
const ffmpeg = require('fluent-ffmpeg')
ffmpeg.setFfmpegPath(ffmpegPath)
const pLimit = require('p-limit')

if (!process.argv[2] || !process.argv[3]) return console.log('Please enter source and destination folder paths\nUse the syntax: node index /source/path /destination/path')
if (process.argv[4] && process.argv[4] < 1 || process.argv[4] < 100) return console.log('Please enter a quality between 1 and 100')

const sourceFolder = process.argv[2]
const destinationFolder = process.argv[3]
const globalQuality = process.argv[4] || 80
const maxWidth = 3000
const maxHeight = 1500
const maxSize = 500000
const limit = pLimit(5) // Limit number of concurrent processes
let compressedFiles = 0

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
async function compressImage(inputPath, outputPath) {
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
                compressedFiles++
            })
    } catch (err) {
        console.error(`Error compressing: ${inputPath}`, err)
    }
}

async function compressVideo(inputPath, outputPath) {
    return new Promise((resolve, reject) => {
        ffmpeg(inputPath)
            .videoBitrate('500k')
            .audioBitrate('128k')
            .on('end', () => {
                console.log(`Compressed video: ${inputPath}`)
                resolve()
            })
            .on('error', (err) => {
                console.error(`Error compressing video: ${inputPath}`, err)
                reject(err)
            })
            .save(outputPath)
    })
}


// Function to read and process files
function processFilesInFolder(folderPath) {
    return new Promise((resolve, reject) => {
        try {
            const files = fs.readdirSync(folderPath)
            const promises = []

            files.forEach((file) => {
                limit(() => {
                    const filePath = path.join(folderPath, file)
                    const stats = fs.statSync(filePath)

                    if (stats.isDirectory()) {
                        // If it's a directory, recursively process its contents
                        const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                        fs.mkdirSync(newDestinationFolder, { recursive: true })
                        promises.push(processFilesInFolder(filePath))
                    } else {
                        const extname = path.extname(file).toLowerCase()
                        // If it's a file, you can check if it's an image and compress it
                        if (['.jpg', '.jpeg', '.png', '.gif'].includes(extname)) {
                            // Create the corresponding subfolder structure in the destination folder
                            const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                            fs.mkdirSync(newDestinationFolder, { recursive: true })

                            // Construct the destination file path
                            const destinationFilePath = path.join(newDestinationFolder, file)

                            // Process and compress the image
                            promises.push(compressImage(filePath, destinationFilePath))
                        } else if (['.mp4', '.avi', '.mkv'].includes(extname)) {
                            // Handle video processing
                            const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                            fs.mkdirSync(newDestinationFolder, { recursive: true })
                            const destinationFilePath = path.join(newDestinationFolder, file)
                            promises.push(compressVideo(filePath, destinationFilePath))
                        } else {
                            // If it's not an image, simply copy it to the destination folder
                            const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                            fs.mkdirSync(newDestinationFolder, { recursive: true })
                            fs.copyFileSync(filePath, path.join(newDestinationFolder, file))
                        }
                    }
                })
            })

            // Wait for all promises to resolve before completing this operation
            Promise.all(promises)
                .then(() => {
                    resolve()
                })
                .catch((err) => {
                    reject(err)
                })
        } catch (err) {
            reject(err)
        }
    })
}

// Start processing files from the source folder
console.log('Reading source and compressing...')
processFilesInFolder(sourceFolder)
    .then(() => {
        console.log(`Compressed ${compressedFiles} files.`)
    })
    .catch((err) => {
        console.error('An error occurred while running the converter', err)
    })
