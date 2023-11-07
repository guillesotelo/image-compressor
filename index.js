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
const compressVideoFlag = process.argv[5] === '--v'
const maxWidth = 3000
const maxHeight = 1500
const maxSize = 500000
const limit = pLimit(5) // Limit number of concurrent processes
let compressedFiles = 0
let errors = 0

sharp.cache(false)
sharp.concurrency(1)

sharp.queue.on('task', (info) => {
    if (info.format === 'jpeg' || info.format === 'png' || info.format === 'webp' || info.format === 'gif') {
        info.options.quality = globalQuality
    }
})

async function compressImage(inputPath, outputPath) {
    try {
        await sharp(inputPath, { failOn: 'truncated' })
            .resize({
                width: maxWidth,
                height: maxHeight,
                fit: 'inside',
                withoutEnlargement: true
            })
            .toBuffer({ resolveWithObject: true, size: maxSize })
            .then(({ data, info }) => {
                fs.writeFileSync(outputPath, data)
                // console.log(`Compressed: ${inputPath}`)
                compressedFiles++
            })
    } catch (err) {
        errors++
        console.error(`Error compressing: ${inputPath}`, err)
    }
}

async function compressVideo(inputPath, outputPath) {
    return new Promise((resolve, reject) => {
        try {
            ffmpeg(inputPath)
                .videoBitrate('500k')
                .audioBitrate('128k')
                .on('end', () => {
                    // console.log(`Compressed video: ${inputPath}`)
                    resolve()
                })
                .save(outputPath)
        } catch (error) {
            errors++
            console.error(`Error compressing video: ${inputPath}`, error)
            reject(error)
        }
    })
}

function processFilesInFolder(folderPath) {
    return new Promise((resolve, reject) => {
        try {
            const files = fs.readdirSync(folderPath)
            const promises = []

            files.forEach((file, i) => {
                const filePath = path.join(folderPath, file)
                const stats = fs.statSync(filePath)

                if (stats.isDirectory()) {
                    const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                    fs.mkdirSync(newDestinationFolder, { recursive: true })
                    promises.push(processFilesInFolder(filePath))
                } else {
                    const extname = path.extname(file).toLowerCase()

                    if (['.jpg', '.jpeg', '.png', '.gif'].includes(extname)) {
                        const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                        fs.mkdirSync(newDestinationFolder, { recursive: true })
                        const destinationFilePath = path.join(newDestinationFolder, file)
                        promises.push(limit(() => compressImage(filePath, destinationFilePath)))
                    } else if (['.mp4', '.avi', '.mkv'].includes(extname) && compressVideoFlag) {
                        const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                        fs.mkdirSync(newDestinationFolder, { recursive: true })
                        const destinationFilePath = path.join(newDestinationFolder, file)
                        promises.push(limit(() => compressVideo(filePath, destinationFilePath)))
                    } else {
                        const newDestinationFolder = path.join(destinationFolder, path.relative(sourceFolder, folderPath))
                        fs.mkdirSync(newDestinationFolder, { recursive: true })
                        fs.copyFileSync(filePath, path.join(newDestinationFolder, file))
                    }
                }
                console.log(`Processing [${i + 1}/${files.length - 1}]`)
            })

            Promise.all(promises)
                .then(() => {
                    resolve()
                })
                .catch((err) => {
                    reject(err)
                })
        } catch (err) {
            console.error('An error occurred while converting the files', err)
            reject(err)
        }
    })
}

console.log('Reading source and compressing...')
processFilesInFolder(sourceFolder)
    .then(() => {
        console.log(`Compressed ${compressedFiles} files.`)
    })
    .catch((err) => {
        console.error('An error occurred while converting the files', err)
    })
