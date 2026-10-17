// Small SDL/OpenGL host for libprojectM's C API. All graphics stay on this
// process's main thread, including on macOS. stdout is exclusively RGB data.
#include <SDL.h>
#ifdef __APPLE__
#include <OpenGL/gl3.h>
#else
#define GL_GLEXT_PROTOTYPES
#include <SDL_opengl.h>
#endif
#include <projectM-4/projectM.h>
#include <algorithm>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

int main(int argc, char** argv) {
    SDL_Window* window = nullptr;
    SDL_GLContext context = nullptr;
    projectm_handle engine = nullptr;
    int result = 0;
    try {
        if (argc != 5) throw std::runtime_error("usage: led-projectm SIZE FPS PRESET_LIST TEXTURES");
        const int size = std::stoi(argv[1]), fps = std::stoi(argv[2]);
        if (size < 1 || size > 128 || fps < 1 || fps > 120)
            throw std::runtime_error("invalid display size or FPS");
        const int scale = 4, resolution = size * scale;
        std::vector<std::string> presets;
        std::ifstream list(argv[3]);
        for (std::string line; std::getline(list, line);) if (!line.empty()) presets.push_back(line);
        if (presets.empty()) throw std::runtime_error("no MilkDrop presets found");
        if (SDL_Init(SDL_INIT_VIDEO) != 0) throw std::runtime_error(SDL_GetError());
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 3);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_CORE);
        SDL_GL_SetAttribute(SDL_GL_CONTEXT_FLAGS, SDL_GL_CONTEXT_FORWARD_COMPATIBLE_FLAG);
        SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 0);
        window = SDL_CreateWindow("LED projectM", SDL_WINDOWPOS_UNDEFINED, SDL_WINDOWPOS_UNDEFINED,
                                  resolution, resolution, SDL_WINDOW_OPENGL | SDL_WINDOW_HIDDEN);
        if (!window) throw std::runtime_error(SDL_GetError());
        context = SDL_GL_CreateContext(window);
        if (!context) throw std::runtime_error(SDL_GetError());
        SDL_GL_SetSwapInterval(0);
        engine = projectm_create();
        if (!engine) throw std::runtime_error("could not initialize libprojectM");
        projectm_set_window_size(engine, resolution, resolution);
        projectm_set_mesh_size(engine, 48, 48);
        projectm_set_fps(engine, fps);
        projectm_set_preset_locked(engine, true);
        projectm_set_soft_cut_duration(engine, 2.0);
        const char* textures[] = {argv[4]};
        projectm_set_texture_search_paths(engine, textures, 1);
        projectm_set_preset_switch_failed_event_callback(engine,
            [](const char*, const char* message, void*) { std::cerr << "PRESET_ERROR " << message << std::endl; }, nullptr);
        std::vector<uint8_t> high(resolution * resolution * 3), low(size * size * 3);
        std::vector<float> pcm;
        glPixelStorei(GL_PACK_ALIGNMENT, 1);
        std::cerr << "READY" << std::endl;
        int previous = -1;
        // Local pipe protocol: three native uint32 values (sample count,
        // preset index, selection revision), then mono float32 PCM.
        uint32_t header[3], revision = 0;
        while (std::cin.read(reinterpret_cast<char*>(header), sizeof(header))) {
            if (header[0] > 48000 || header[1] >= presets.size())
                throw std::runtime_error("invalid renderer request");
            pcm.resize(header[0]);
            if (!std::cin.read(reinterpret_cast<char*>(pcm.data()), pcm.size() * sizeof(float))) break;
            SDL_PumpEvents();
            if (previous != static_cast<int>(header[1]) || revision != header[2]) {
                projectm_load_preset_file(engine, presets[header[1]].c_str(), previous >= 0);
                previous = static_cast<int>(header[1]);
                revision = header[2];
            }
            const auto maximum = projectm_pcm_get_max_samples();
            for (size_t offset = 0; offset < pcm.size(); offset += maximum)
                projectm_pcm_add_float(engine, pcm.data() + offset,
                    std::min<size_t>(maximum, pcm.size() - offset), PROJECTM_MONO);
            projectm_opengl_render_frame(engine);
            // projectM restores the draw framebuffer, but leaves its internal
            // read framebuffer bound. Capture the final composite on screen.
            glBindFramebuffer(GL_READ_FRAMEBUFFER, 0);
            glReadBuffer(GL_FRONT);
            glReadPixels(0, 0, resolution, resolution, GL_RGB, GL_UNSIGNED_BYTE, high.data());
            // Average every 4x4 block and flip OpenGL's bottom-up image.
            // This preserves fine waves and particles without aliasing.
            for (int y = 0; y < size; ++y) for (int x = 0; x < size; ++x)
                for (int c = 0; c < 3; ++c) {
                    unsigned sum = 0;
                    for (int dy = 0; dy < scale; ++dy) for (int dx = 0; dx < scale; ++dx)
                        sum += high[(((size - 1 - y) * scale + dy) * resolution + x * scale + dx) * 3 + c];
                    low[(y * size + x) * 3 + c] = (sum + 8) / 16;
                }
            if (!std::cout.write(reinterpret_cast<char*>(low.data()), low.size()).flush()) break;
        }
    } catch (const std::exception& error) {
        std::cerr << "ERROR " << error.what() << std::endl;
        result = 1;
    }
    if (engine) projectm_destroy(engine);
    if (context) SDL_GL_DeleteContext(context);
    if (window) SDL_DestroyWindow(window);
    SDL_Quit();
    return result;
}
