const fs = require('fs')
const path = require('path')
const sharp = require('sharp')
const ffmpegPath = require('@ffmpeg-installer/ffmpeg').path
const ffmpeg = require('fluent-ffmpeg')
ffmpeg.setFfmpegPath(ffmpegPath)
const pLimit = require('p-limit')

/* =======================
   ARGUMENTS & VALIDATION
   ======================= */

if (!process.argv[2] || !process.argv[3]) {
    console.log('Please enter source and destination folder paths')
    console.log('Usage: node index /source/path /destination/path [quality] [--v]')
    process.exit(1)
}

if (process.argv[4] && (process.argv[4] < 1 || process.argv[4] > 100)) {
    console.log('Please enter a quality between 1 and 100')
    process.exit(1)
}

const sourceFolder = process.argv[2]
const destinationFolder = process.argv[3]
const globalQuality = Number(process.argv[4]) || 80
const compressVideoFlag = process.argv[5] === '--v'

/* =======================
   IMAGE / VIDEO SETTINGS
   ======================= */

const maxWidth = 3000
const maxHeight = 1500
const maxSize = 500000 // bytes
const limit = pLimit(5)

let compressedFiles = 0
let errors = 0

sharp.cache(false)
sharp.concurrency(1)

/* Apply quality globally */
sharp.queue.on('task', (info) => {
    if (['jpeg', 'png', 'webp', 'gif'].includes(info.format)) {
        info.options.quality = globalQuality
    }
})

/* =======================
   IMAGE COMPRESSION
   ======================= */

async function compressImage(inputPath, outputPath) {
    try {
        const { data } = await sharp(inputPath, { failOn: 'truncated' })
            .rotate() // ✅ EXIF-aware: rotates ONLY if needed
            .resize({
                width: maxWidth,
                height: maxHeight,
                fit: 'inside',
                withoutEnlargement: true
            })
            .withMetadata() // ✅ preserve EXIF (date, GPS, camera, etc.)
            .toBuffer({ resolveWithObject: true, size: maxSize })

        fs.writeFileSync(outputPath, data)
        compressedFiles++
    } catch (err) {
        errors++
        console.error(`Error compressing image: ${inputPath}`, err.message)
    }
}

/* =======================
   VIDEO COMPRESSION
   ======================= */

function compressVideo(inputPath, outputPath) {
    return new Promise((resolve, reject) => {
        try {
            ffmpeg(inputPath)
                .videoBitrate('500k')
                .audioBitrate('128k')
                .on('end', resolve)
                .on('error', (err) => {
                    errors++
                    console.error(`Error compressing video: ${inputPath}`, err.message)
                    reject(err)
                })
                .save(outputPath)
        } catch (error) {
            errors++
            reject(error)
        }
    })
}

/* =======================
   RECURSIVE FILE WALK
   ======================= */

function processFilesInFolder(folderPath) {
    return new Promise((resolve, reject) => {
        try {
            const files = fs.readdirSync(folderPath)
            const promises = []

            files.forEach((file, i) => {
                const filePath = path.join(folderPath, file)
                const stats = fs.statSync(filePath)

                const relativePath = path.relative(sourceFolder, folderPath)
                const targetFolder = path.join(destinationFolder, relativePath)
                fs.mkdirSync(targetFolder, { recursive: true })

                if (stats.isDirectory()) {
                    promises.push(processFilesInFolder(filePath))
                    return
                }

                const ext = path.extname(file).toLowerCase()
                const outputPath = path.join(targetFolder, file)

                if (['.jpg', '.jpeg', '.png', '.gif'].includes(ext)) {
                    promises.push(limit(() => compressImage(filePath, outputPath)))
                }
                else if (['.mp4', '.avi', '.mkv'].includes(ext) && compressVideoFlag) {
                    promises.push(limit(() => compressVideo(filePath, outputPath)))
                }
                else {
                    fs.copyFileSync(filePath, outputPath)
                }

                console.log(`Processing [${i + 1}/${files.length}]`)
            })

            Promise.all(promises).then(resolve).catch(reject)
        } catch (err) {
            reject(err)
        }
    })
}

/* =======================
   START
   ======================= */

console.log('Reading source and compressing...')

processFilesInFolder(sourceFolder)
    .then(() => {
        console.log(`\nDone.`)
        console.log(`Compressed images: ${compressedFiles}`)
        console.log(`Errors: ${errors}`)
    })
    .catch((err) => {
        console.error('Fatal error:', err)
    })
