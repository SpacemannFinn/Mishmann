📻 Mishmann Player
[![Hippocratic License HL3-FULL](https://img.shields.io/static/v1?label=Hippocratic%20License&message=HL3-FULL&labelColor=5e2751&color=bc8c3d)](https://firstdonoharm.dev/version/3/0/full.html)

A premium, tactile digital minimalist music deck designed for single-board computers (specifically the Radxa Zero), utilizing an ILI9488 $480 \times 320$ IPS SPI display and a highly physical, five-button interface.📖 Project OverviewThe Mishmann Player bridges the gap between the physical warmth of classic vintage tape decks (such as the legendary 1979 Sony Walkman TPS-L2) and the hyper-optimized efficiency of modern digital systems.Designed to operate entirely headless on embedded hardware, the player discards bloated touch-screen paradigms in favor of a strictly linear, five-button physical interface. It features dynamic hardware color extraction, zero-ghosting see-through HUDs, and an innovative split-screen library interface with a smart album collage engine.+-------------------------------------------------------------+
|                                                             |
|                      +-----------------+                    |
|                      |                 |     (SHUFFLE ICON) |
|                      |    ALBUM ART    |         [ x ]      |
|                      |     160x160     |                    |
|                      |                 |                    |
|                      +-----------------+                    |
|                                                             |
|                       ( )-----------( )                     |
|                      SPOOL1  TAPE   SPOOL2                  |
|                                                             |
|  TITLE                                                      |
|  ARTIST                                                TIME |
+-------------------------------------------------------------+
✨ Key Features🎨 Dynamic HSV Palette Engine: No static skins. The player extracts dominant colors from the metadata's album art in real-time, mathematically scaling Hue, Saturation, and Value (HSV) to output sophisticated dark backgrounds and high-contrast complementary neon accents.📼 Mechanical Cassette Spools: Mimics mechanical hubs with rotating Twin Single-Bar Spools. To prevent SPI bottlenecks, 36 frames are pre-rendered into RAM at boot, providing a smooth $25\text{fps}$ spin during active playback that pauses instantly.🌓 Concept 2 Split-Screen Library: Leverages the $480 \times 320$ horizontal aspect ratio. The left pane shows a crisp 44px text list, while the right pane renders an interactive "Deck Preview" with a feathered drop shadow, custom metadata, and a dynamic album art compilation collage (50/50 vertical split for 2 albums, 2x2 grid for 3-4 albums).🎛️ See-Through Volume HUD: Displays volume changes as a translucent overlay directly on top of the album artwork using alpha-composited slices. When the HUD auto-hides, it perfectly restores the pristine backdrop slice—resulting in zero background ghosting.📶 Phone-Assisted Wi-Fi Bootstrap & Web Portal: If no connection is found at boot, the player starts a memorable access point hotspot (Walkman Setup) and guides the user on-screen to connect with their phone. Once connected, they can upload audio tracks (MP3, FLAC, M4A, etc.) or scan and connect to local Wi-Fi networks via a mobile-friendly dark web console.🏷️ MusicBrainz Auto-Tagging: An asynchronous worker thread (genre_fill.py) automatically polls the MusicBrainz API when connected to Wi-Fi to identify missing genres, writing them permanently back to files using mutagen without blocking the audio stream.🩺 Rigorous Boot Diagnostic Checklist: Before launching, boot.py performs rigorous self-checks (SPI display, claiming GPIO lines, backlight PWM, PulseAudio pipeline, music directories) and displays a hardware check sequence to the user.🛠️ Hardware & Pinout ConfigurationThis project was built for the Radxa Zero utilizing the Linux Kernel GPIO Character Device API (gpiod) and spidev.Pinout Table (Radxa Zero GPIO Header)Hardware ComponentDevice Pin / LabelRadxa Zero Header PinLine Number (gpiochip3)ILI9488 DisplaySPI MOSIPin 19 (GPIOC_1)- (Hardware SPI)SPI SCLKPin 23 (GPIOC_3)- (Hardware SPI)SPI CS0Pin 24 (GPIOC_4)- (Hardware SPI)Data / Command (DC)Pin 11 (GPIOA_1)Line 1Reset (RST)Pin 21 (GPIOA_8)Line 8Backlight PWMPin 18 (GPIOB_10)Line 10 (PWM Channel 0)Tactile ButtonsPlay / PausePin 13 (GPIOA_2)Line 2Next TrackPin 38 (GPIOA_20)Line 20Previous TrackPin 16 (GPIOA_4)Line 4Volume UpPin 29 (GPIOA_11)Line 11Volume DownPin 31 (GPIOA_12)Line 12📸 Interface Screenshots(Place your device screen captures in /assets/ and link them below)1. Diagnostic Boot Checks (boot.py)[Insert Screenshot: /assets/boot_checklist.png]
Visual description: A technical, terminal-like diagnostic checklist on a dark slate background. Displays real-time pass/fail markers for core interfaces alongside a beautiful glowing orange underline indicator.2. Layout 5 Playback Display[Insert Screenshot: /assets/playback_now.png]
Visual description: The central album art box stands out with a modern, blurred $8\text{px}$ drop shadow and $2\text{px}$ black border. Below, the dual single-bar spools are rotating, and the track titles/timer glow in the split-complementary accent color.3. Concept 2 Split-View Library[Insert Screenshot: /assets/split_library.png]
Visual description: On the left, the selected row is highlighted in a vibrant, dynamic theme color. On the right, a split collage showing two album covers represents an artist with multiple records, accompanied by their genre at the bottom.4. Translucent Volume Overlay[Insert Screenshot: /assets/volume_overlay.png]
Visual description: Shows a clean, semi-transparent dark volume track laid right on top of the album artwork. When closed, the overlay disappears cleanly leaving no artifact trails or ghost lines.📂 System ArchitectureThe software is structured as a series of lightweight, highly focused modules:                  +--------------------------------+
                  |            boot.py             |
                  |  (Initializes & self-checks)   |
                  +---------------+----------------+
                                  |
                                  v
                  +--------------------------------+
                  |        music_player.py         | <------+
                  |   (Primary UI and state loop)  |        |
                  +-------+--------------+---------+        |
                          |              |                  |
   (Controls audio stream)|              | (BlueZ D-Bus)    | (Signals rescan)
                          v              v                  |
                  +---------------+ +----+---------+        |
                  | GStreamer API | | bt_manager.py |        |
                  +---------------+ +--------------+        |
                                                            |
                  +--------------------------------+        |
                  |        upload_server.py        | -------+
                  |  (Flask server & Wi-Fi Setup)  |
                  +---------------+----------------+
                                  |
            (Polls Wi-Fi)         v
                  +---------------+
                  | genre_fill.py | (MusicBrainz thread)
                  +---------------+
boot.py: The gatekeeper. Prevents initialization failures from quietly lingering by halting execution and drawing a fatal screen if GStreamer or the display cannot be established.music_player.py: Manages the SPI address windows, PIL drawings, spool animations, button pollers, and GStreamer state transitions.upload_server.py: Houses the multi-threaded Flask backend, serving a mobile-friendly dropzone, a library deleting/management page, and a deep NMCLI integration manager to configure device Wi-Fi over the air.bt_manager.py: A native D-Bus client talking directly to BlueZ to scan, trust, pair, and connect to bluetooth headphones and speakers without calling sluggish shell scripts.genre_fill.py: Auto-populates missing genre tags asynchronously when Wi-Fi becomes active.⚙️ Installation & Running1. System DependenciesThe player requires GStreamer, GLib, PulseAudio, and several system libraries to connect over D-Bus and claim hardware lines. Install them on your Radxa Zero running Debian/Ubuntu:sudo apt update
sudo apt install python3-pip python3-dbus python3-gi python3-flask python3-mutagen python3-pil
sudo apt install gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-pulseaudio
2. Clone the Repository & Configure Permissionsgit clone [https://github.com/yourusername/mishmann-player.git](https://github.com/yourusername/mishmann-player.git)
cd mishmann-player

# Create music directory
mkdir -p /home/rock/music
Note: Since the server interacts with NetworkManager via nmcli to establish hotspots and connect to networks, make sure the user running the script has appropriate system permissions or sudo privileges.3. LaunchingTo start the entire player platform with full diagnostic checks, execute:python3 boot.py
🎨 Visual Calibration & Aesthetic SpecsIf you are tweaking the UI or using this repository as a boilerplate, keep these design invariants in mind (calibrated for the ILI9488 display):Blur Strengths: Any blur radius above $8$ causes major frame drops on SPI when rendering on an ARM Cortex-A53. Stick to a radius of $5$ to $6$ for shadows.Dynamic Value Limits: If background value ($V$) falls below $0.3$, menu lines are illegible. If it rises above $0.6$, neon accents lose their glow. The extractor in music_player.py clamps these values dynamically.Double-Click Button Protection: Tactile pushbuttons suffer from physical bounce. The ButtonHandler includes software debouncing and event-batching logic, grouping rapid volume-button taps into a single Delta volume shift before sending them to GStreamer and the SPI screen.📄 LicenseThis project is licensed under the MIT License. See the LICENSE file for details.
