// Apple Vision multi-card detector for photos that hold several business cards.
//
// usage: vision_cards <input-image> <output-dir>
// Prints JSON {image_width, image_height, rectangles:[{index, box, confidence,
// crop_path, corners}]}. Coordinates are normalized with a top-left origin;
// corners are TL, TR, BR, BL. Each rectangle is perspective-corrected at the
// input's native resolution. Filtering is done in bizcard_intake/multicard.py.
import Foundation
import Vision
import CoreImage
import ImageIO
import UniformTypeIdentifiers

struct RectangleResult: Encodable {
    let index: Int
    let box: [Double]
    let confidence: Double
    let crop_path: String
    let corners: [[Double]]
}

struct ProbeResult: Encodable {
    let image_width: Int
    let image_height: Int
    let rectangles: [RectangleResult]
}

enum ProbeError: Error, CustomStringConvertible {
    case failure(String)
    var description: String {
        switch self { case .failure(let message): return message }
    }
}

func run() throws {
    guard CommandLine.arguments.count == 3 else {
        throw ProbeError.failure("usage: probe <input-image> <output-dir>")
    }
    let sourceURL = URL(fileURLWithPath: CommandLine.arguments[1])
    let outputURL = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
    guard let source = CGImageSourceCreateWithURL(sourceURL as CFURL, nil),
          let decoded = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw ProbeError.failure("cannot decode input image")
    }
    let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
    let orientation = (properties?[kCGImagePropertyOrientation] as? NSNumber)?.int32Value ?? 1
    guard (1...8).contains(orientation) else {
        throw ProbeError.failure("invalid EXIF orientation")
    }
    let oriented = CIImage(cgImage: decoded).oriented(forExifOrientation: orientation)
    let image = oriented.transformed(by: CGAffineTransform(
        translationX: -oriented.extent.minX, y: -oriented.extent.minY))
    let context = CIContext()
    guard let visionImage = context.createCGImage(image, from: image.extent) else {
        throw ProbeError.failure("cannot render oriented input image")
    }
    let width = visionImage.width
    let height = visionImage.height
    let request = VNDetectRectanglesRequest()
    request.maximumObservations = 32
    request.minimumSize = 0.06
    request.minimumConfidence = 0.5
    request.minimumAspectRatio = 0.3
    request.maximumAspectRatio = 1.0
    request.quadratureTolerance = 35
    try VNImageRequestHandler(cgImage: visionImage, orientation: .up).perform([request])
    guard let observations = request.results else {
        throw ProbeError.failure("rectangle detector returned no result collection")
    }

    // Form rows against a stable anchor so the tolerance does not create
    // a non-transitive sorting comparator. Preserve all observations.
    func topY(_ observation: VNRectangleObservation) -> CGFloat {
        1 - observation.boundingBox.maxY
    }
    var pending = observations.enumerated().sorted {
        if topY($0.element) != topY($1.element) {
            return topY($0.element) < topY($1.element)
        }
        if $0.element.boundingBox.minX != $1.element.boundingBox.minX {
            return $0.element.boundingBox.minX < $1.element.boundingBox.minX
        }
        return $0.offset < $1.offset
    }
    var ordered: [VNRectangleObservation] = []
    while let first = pending.first {
        let anchor = topY(first.element)
        let rowCount = pending.prefix { topY($0.element) - anchor <= 0.03 }.count
        let row = pending.prefix(rowCount).sorted {
            if $0.element.boundingBox.minX != $1.element.boundingBox.minX {
                return $0.element.boundingBox.minX < $1.element.boundingBox.minX
            }
            if topY($0.element) != topY($1.element) {
                return topY($0.element) < topY($1.element)
            }
            return $0.offset < $1.offset
        }
        ordered.append(contentsOf: row.map(\.element))
        pending.removeFirst(rowCount)
    }

    try FileManager.default.createDirectory(at: outputURL, withIntermediateDirectories: true)
    var results: [RectangleResult] = []
    for (index, observation) in ordered.enumerated() {
        guard let filter = CIFilter(name: "CIPerspectiveCorrection") else {
            throw ProbeError.failure("CIPerspectiveCorrection unavailable")
        }
        filter.setValue(image, forKey: kCIInputImageKey)
        let points = [observation.topLeft, observation.topRight,
                      observation.bottomRight, observation.bottomLeft]
        for (key, point) in zip(
            ["inputTopLeft", "inputTopRight", "inputBottomRight", "inputBottomLeft"], points) {
            filter.setValue(CIVector(x: point.x * CGFloat(width),
                                    y: point.y * CGFloat(height)), forKey: key)
        }
        guard let corrected = filter.outputImage,
              !corrected.extent.isEmpty, !corrected.extent.isInfinite,
              let crop = context.createCGImage(corrected, from: corrected.extent) else {
            throw ProbeError.failure("cannot rectify candidate \(index)")
        }
        let cropURL = outputURL.appendingPathComponent(String(format: "candidate-%03d.jpg", index))
        guard let destination = CGImageDestinationCreateWithURL(
            cropURL as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw ProbeError.failure("cannot create JPEG for candidate \(index)")
        }
        CGImageDestinationAddImage(destination, crop,
            [kCGImageDestinationLossyCompressionQuality: 0.95] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else {
            throw ProbeError.failure("cannot write JPEG for candidate \(index)")
        }
        let box = observation.boundingBox
        results.append(RectangleResult(
            index: index,
            box: [Double(box.minX), Double(1 - box.maxY), Double(box.width), Double(box.height)],
            confidence: Double(observation.confidence),
            crop_path: cropURL.path,
            corners: points.map { [Double($0.x), Double(1 - $0.y)] }))
    }
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    let encoded = try encoder.encode(ProbeResult(
        image_width: width, image_height: height, rectangles: results))
    FileHandle.standardOutput.write(encoded)
    FileHandle.standardOutput.write(Data([10]))
}

do {
    try run()
} catch {
    FileHandle.standardError.write(Data("probe: \(error)\n".utf8))
    exit(1)
}
