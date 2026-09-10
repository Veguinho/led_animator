import AVFoundation
import CoreAudio
import Foundation

private let sampleRate = 48_000.0

struct CaptureError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

private func check(_ status: OSStatus, _ operation: String) throws {
    guard status == noErr else {
        throw CaptureError(message: "\(operation) failed (Core Audio status \(status))")
    }
}

// The Python renderer always consumes mono Float32 at 48 kHz, regardless of
// the sample rate/channel layout of the Mac's current output device.
final class PCMConverter {
    let inputFormat: AVAudioFormat
    private let outputFormat: AVAudioFormat
    private let converter: AVAudioConverter

    init(inputFormat: AVAudioFormat) throws {
        self.inputFormat = inputFormat
        guard let outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32, sampleRate: sampleRate,
            channels: 1, interleaved: false
        ), let converter = AVAudioConverter(from: inputFormat, to: outputFormat) else {
            throw CaptureError(message: "could not convert system audio to mono 48 kHz PCM")
        }
        self.outputFormat = outputFormat
        self.converter = converter
    }

    func convert(_ input: AVAudioPCMBuffer) throws -> Data {
        let capacity = AVAudioFrameCount(
            ceil(Double(input.frameLength) * sampleRate / inputFormat.sampleRate) + 64
        )
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            throw CaptureError(message: "could not allocate the audio conversion buffer")
        }
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, state in
            if supplied {
                state.pointee = .noDataNow
                return nil
            }
            supplied = true
            state.pointee = .haveData
            return input
        }
        if status == .error {
            throw error ?? CaptureError(message: "could not convert system audio") as NSError
        }
        guard let samples = output.floatChannelData?[0] else { return Data() }
        return Data(bytes: samples, count: Int(output.frameLength) * MemoryLayout<Float>.size)
    }
}

@available(macOS 14.2, *)
final class SystemAudioCapture {
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var device = AudioObjectID(kAudioObjectUnknown)
    private var ioProc: AudioDeviceIOProcID?
    private let captureQueue = DispatchQueue(label: "led-animator.audio-tap")
    private let writerQueue = DispatchQueue(label: "led-animator.audio-writer")
    private let availableBuffers = DispatchSemaphore(value: 3)
    private var pcm: PCMConverter?
    var onError: ((Error) -> Void)?

    func start() throws {
        // A global mono tap reads playback only. No displays, windows, screen
        // streams, microphones, or changes to the user's default audio device.
        let description = CATapDescription(monoGlobalTapButExcludeProcesses: [])
        description.name = "LED Visualizer System Audio"
        description.uuid = UUID()
        description.isPrivate = true
        description.muteBehavior = .unmuted
        try check(AudioHardwareCreateProcessTap(description, &tap), "creating the system-audio tap")

        var address = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        try check(AudioObjectGetPropertyData(tap, &address, 0, nil, &size, &asbd), "reading the audio format")
        guard asbd.mSampleRate > 0, asbd.mBytesPerFrame > 0,
              let format = AVAudioFormat(streamDescription: &asbd) else {
            throw CaptureError(message: "the system-audio tap reported an unsupported PCM format")
        }
        pcm = try PCMConverter(inputFormat: format)

        let configuration: [String: Any] = [
            kAudioAggregateDeviceNameKey: "LED Visualizer Audio Capture",
            kAudioAggregateDeviceUIDKey: "local.led-animator.capture.\(UUID().uuidString)",
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapUIDKey: description.uuid.uuidString,
                kAudioSubTapDriftCompensationKey: true
            ]]
        ]
        try check(AudioHardwareCreateAggregateDevice(configuration as CFDictionary, &device), "creating the audio capture device")
        try check(AudioDeviceCreateIOProcIDWithBlock(&ioProc, device, captureQueue) {
            [weak self] _, input, _, _, _ in
            self?.receive(input)
        }, "connecting the system-audio tap")
        try check(AudioDeviceStart(device, ioProc), "starting system-audio capture")
    }

    private func receive(_ input: UnsafePointer<AudioBufferList>) {
        guard let pcm, availableBuffers.wait(timeout: .now()) == .success else { return }
        let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        let bytesPerFrame = Int(pcm.inputFormat.streamDescription.pointee.mBytesPerFrame)
        guard let first = source.first, first.mData != nil, first.mDataByteSize > 0 else {
            availableBuffers.signal()
            return
        }
        let frames = AVAudioFrameCount(Int(first.mDataByteSize) / bytesPerFrame)
        guard frames > 0,
              let copied = AVAudioPCMBuffer(pcmFormat: pcm.inputFormat, frameCapacity: frames) else {
            availableBuffers.signal()
            return
        }
        copied.frameLength = frames
        let destination = UnsafeMutableAudioBufferListPointer(copied.mutableAudioBufferList)
        guard source.count == destination.count else {
            availableBuffers.signal()
            return
        }
        for (src, dst) in zip(source, destination) {
            guard let srcData = src.mData, let dstData = dst.mData,
                  src.mDataByteSize >= dst.mDataByteSize else {
                availableBuffers.signal()
                return
            }
            memcpy(dstData, srcData, Int(dst.mDataByteSize))
        }
        // Never block Core Audio on the Python pipe. If Python falls behind,
        // drop new buffers instead of growing a queue of stale audio.
        writerQueue.async { [self] in
            defer { availableBuffers.signal() }
            do {
                let data = try pcm.convert(copied)
                if !data.isEmpty { try FileHandle.standardOutput.write(contentsOf: data) }
            } catch {
                onError?(error)
            }
        }
    }

    func close() {
        if let ioProc {
            AudioDeviceStop(device, ioProc)
            AudioDeviceDestroyIOProcID(device, ioProc)
            self.ioProc = nil
        }
        if device != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(device)
            device = AudioObjectID(kAudioObjectUnknown)
        }
        if tap != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tap)
            tap = AudioObjectID(kAudioObjectUnknown)
        }
    }
}

// Exercise real sample conversion without creating a tap or asking permission.
func testConversion() throws {
    for rate in [44_100.0, 48_000.0, 96_000.0] {
        for channels: AVAudioChannelCount in [1, 2] {
            let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: rate,
                                       channels: channels, interleaved: false)!
            let converter = try PCMConverter(inputFormat: format)
            var converted: [Float] = []
            for block in 0..<20 {
                let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 1024)!
                buffer.frameLength = 1024
                for channel in 0..<Int(channels) {
                    for index in 0..<1024 {
                        buffer.floatChannelData![channel][index] = Float(
                            0.25 * sin(2 * .pi * 440 * Double(block * 1024 + index) / rate)
                        )
                    }
                }
                let data = try converter.convert(buffer)
                converted += data.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) }
            }
            let expected = Int(20 * 1024 * sampleRate / rate)
            guard abs(converted.count - expected) < 256,
                  converted.allSatisfy({ $0.isFinite }),
                  converted.map({ abs($0) }).max()! > 0.20 else {
                throw CaptureError(message: "PCM conversion self-test failed for \(rate) Hz / \(channels) channels")
            }
            let crossings = zip(converted, converted.dropFirst()).filter { $0 <= 0 && $1 > 0 }.count
            let frequency = Double(crossings) * sampleRate / Double(converted.count)
            guard abs(frequency - 440) < 5 else {
                throw CaptureError(message: "PCM conversion changed the tone frequency: \(frequency)")
            }
        }
    }
    FileHandle.standardError.write(Data("PCM conversion self-test passed (44.1/48/96 kHz, mono/stereo).\n".utf8))
}

if CommandLine.arguments.contains("--self-test") {
    do {
        try testConversion()
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("error: \(error.localizedDescription)\n".utf8))
        exit(1)
    }
}

guard #available(macOS 14.2, *) else {
    FileHandle.standardError.write(Data("error: audio-only capture requires macOS 14.2 or newer\n".utf8))
    exit(1)
}

let capture = SystemAudioCapture()
func stop(_ status: Int32, error: Error? = nil) {
    if let error {
        FileHandle.standardError.write(Data("error: \(error.localizedDescription)\n".utf8))
    }
    capture.close()
    exit(status)
}
signal(SIGPIPE, SIG_IGN)
signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let terminateSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
let interruptSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
terminateSource.setEventHandler { stop(0) }
interruptSource.setEventHandler { stop(0) }
terminateSource.resume()
interruptSource.resume()
capture.onError = { error in DispatchQueue.main.async { stop(1, error: error) } }
DispatchQueue.global(qos: .userInitiated).async {
    do {
        try capture.start()
        FileHandle.standardError.write(Data("READY\n".utf8))
    } catch {
        DispatchQueue.main.async { stop(1, error: error) }
    }
}
dispatchMain()
