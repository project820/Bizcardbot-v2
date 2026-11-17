// Apple Vision OCR helper.
//
// Prints JSON:
// [{"text":"...", "confidence":0.98, "topLeft":{"x":0.1,"y":0.9},
//   "topRight":{"x":0.8,"y":0.9}}, ...]

import Foundation
import Vision

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

let args = CommandLine.arguments
guard args.count == 2 else {
    fail("usage: vision_ocr <input-image>")
}

let inputURL = URL(fileURLWithPath: args[1])

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
request.recognitionLanguages = ["ko-KR", "en-US"]

let handler = VNImageRequestHandler(url: inputURL, options: [:])
try handler.perform([request])

let rows: [[String: Any]] = (request.results ?? []).compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else {
        return nil
    }
    return [
        "text": candidate.string,
        "confidence": candidate.confidence,
        "topLeft": ["x": observation.topLeft.x, "y": observation.topLeft.y],
        "topRight": ["x": observation.topRight.x, "y": observation.topRight.y]
    ]
}

let data = try JSONSerialization.data(withJSONObject: rows, options: [])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
