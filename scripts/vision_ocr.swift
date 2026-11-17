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

// Calibration: Korean fixtures have >=30 Hangul scalars; six synthetic
// JA/SC/TC cards hallucinate at most 12 in the v2.1.1 pass. Keep a margin.
let koreanHangulThreshold = 20
// English fixtures have zero kana/Han; horizontal CJK cards have >=22.
let cjkCharacterThreshold = 8

func hangulCount(_ observations: [VNRecognizedTextObservation]) -> Int {
    return observations.reduce(0) { total, observation in
        total + (observation.topCandidates(1).first?.string.unicodeScalars.filter {
            (0x1100...0x11FF).contains($0.value) || (0x3130...0x318F).contains($0.value) ||
            (0xA960...0xA97F).contains($0.value) || (0xAC00...0xD7FF).contains($0.value)
        }.count ?? 0)
    }
}

func cjkCount(_ observations: [VNRecognizedTextObservation]) -> Int {
    return observations.reduce(0) { total, observation in
        total + (observation.topCandidates(1).first?.string.unicodeScalars.filter {
            (0x3040...0x30FF).contains($0.value) || (0x3400...0x9FFF).contains($0.value) ||
            (0xF900...0xFAFF).contains($0.value) || (0xFF66...0xFF9F).contains($0.value) ||
            (0x20000...0x323AF).contains($0.value)
        }.count ?? 0)
    }
}

// Both passes share one helper process. Pass 1 options stay exactly v2.1.1.
var adopted = request.results ?? []
if hangulCount(adopted) < koreanHangulThreshold {
    let cjkRequest = VNRecognizeTextRequest()
    cjkRequest.recognitionLevel = .accurate
    cjkRequest.usesLanguageCorrection = true
    if #available(macOS 13.0, *) {
        cjkRequest.automaticallyDetectsLanguage = true
    }
    // Language/revision support varies on older macOS. Retain pass 1 if the
    // fallback cannot run; a fallback failure must not discard working OCR.
    if #available(macOS 12.0, *),
       let supported = try? cjkRequest.supportedRecognitionLanguages() {
        let languages = ["ja-JP", "zh-Hans", "zh-Hant"].filter { supported.contains($0) }
        if !languages.isEmpty {
            cjkRequest.recognitionLanguages = languages
            do {
                try handler.perform([cjkRequest])
                let candidates = cjkRequest.results ?? []
                if cjkCount(candidates) >= cjkCharacterThreshold {
                    // Recover Korean lines missing from the CJK pass. A Hangul
                    // hallucination over the same kana/Han line must not return.
                    let cjkLines = candidates.filter { cjkCount([$0]) > 0 }
                    let retained = adopted.filter { line in
                        guard hangulCount([line]) > 0 else { return false }
                        let blockers = cjkLines + candidates.filter {
                            $0.topCandidates(1).first?.string == line.topCandidates(1).first?.string
                        }
                        return !blockers.contains { candidate in
                            let a = line.boundingBox, b = candidate.boundingBox
                            let intersection = a.intersection(b)
                            guard !intersection.isNull else { return false }
                            let smallerArea = min(a.width * a.height, b.width * b.height)
                            return smallerArea > 0 && intersection.width * intersection.height / smallerArea >= 0.3
                        }
                    }
                    adopted = (candidates + retained).sorted { a, b in
                        if abs(a.boundingBox.midY - b.boundingBox.midY) > 0.015 {
                            return a.boundingBox.midY > b.boundingBox.midY
                        }
                        return a.boundingBox.minX < b.boundingBox.minX
                    }
                }
            } catch {
                // Pass 1 remains usable.
            }
        }
    }
}

let rows: [[String: Any]] = adopted.compactMap { observation in
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
