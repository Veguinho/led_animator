import CoreAudio
import CoreMedia
import Foundation
import ScreenCaptureKit

private let sampleRate = 48_000

@available(macOS 13.0, *)
final class SystemAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private let captureQueue = DispatchQueue(label: "led-animator.system-audio")
    private let output = FileHandle.standardOutput
    private var stream: SCStream?

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false,
            onScreenWindowsOnly: true
        )
        guard let display = content.displays.first else {
            throw CaptureError.noDisplay
        }

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let configuration = SCStreamConfiguration()
        configuration.width = 2
        configuration.height = 2
        configuration.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        configuration.queueDepth = 3
        configuration.showsCursor = false
        configuration.capturesAudio = true
        configuration.excludesCurrentProcessAudio = true
        configuration.sampleRate = sampleRate
        configuration.channelCount = 1

        let stream = SCStream(
            filter: filter,
            configuration: configuration,
            delegate: self
        )
        try stream.addStreamOutput(
            self,
            type: .audio,
            sampleHandlerQueue: captureQueue
        )
        try await stream.startCapture()
        self.stream = stream
    }

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, sampleBuffer.isValid, sampleBuffer.numSamples > 0,
              let format = sampleBuffer.formatDescription,
              let description = CMAudioFormatDescriptionGetStreamBasicDescription(format)
        else {
            return
        }

        let audio = description.pointee
        let isFloat32 = audio.mFormatID == kAudioFormatLinearPCM
            && audio.mBitsPerChannel == 32
            && (audio.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        guard isFloat32 else {
            FileHandle.standardError.write(
                Data("error: system audio was not 32-bit float PCM\n".utf8)
            )
            return
        }

        let maximumBuffers = max(1, Int(audio.mChannelsPerFrame))
        let listSize = MemoryLayout<AudioBufferList>.size
            + (maximumBuffers - 1) * MemoryLayout<AudioBuffer>.size
        let storage = UnsafeMutableRawPointer.allocate(
            byteCount: listSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { storage.deallocate() }
        let list = storage.bindMemory(to: AudioBufferList.self, capacity: 1)
        var retainedBlock: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: list,
            bufferListSize: listSize,
            blockBufferAllocator: kCFAllocatorDefault,
            blockBufferMemoryAllocator: kCFAllocatorDefault,
            flags: 0,
            blockBufferOut: &retainedBlock
        )
        guard status == noErr else { return }

        let buffers = UnsafeMutableAudioBufferListPointer(list)
        guard let buffer = buffers.first, let data = buffer.mData else { return }
        output.write(Data(bytes: data, count: Int(buffer.mDataByteSize)))
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        FileHandle.standardError.write(Data("error: \(error.localizedDescription)\n".utf8))
        exit(1)
    }
}

enum CaptureError: LocalizedError {
    case noDisplay

    var errorDescription: String? {
        "macOS did not report a display to ScreenCaptureKit"
    }
}

guard #available(macOS 13.0, *) else {
    FileHandle.standardError.write(
        Data("error: system audio capture requires macOS 13 or newer\n".utf8)
    )
    exit(1)
}

let capture = SystemAudioCapture()
Task {
    do {
        try await capture.start()
        FileHandle.standardError.write(Data("READY\n".utf8))
    } catch {
        FileHandle.standardError.write(Data("error: \(error.localizedDescription)\n".utf8))
        exit(1)
    }
}
dispatchMain()
