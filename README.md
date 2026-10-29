# Windows Battery Saver Wizard 🔋

A lightweight, open-source Windows utility written in Python (Tkinter) to optimize power profiles, CPU limits, screen refresh rates, and monitor power discharge rate (W) in real-time.

## Features
- 🔋 **Maximum Saver Mode**: Limits CPU max state, disables Turbo Boost, forces 60 Hz display refresh rate, and tunes Wi-Fi/PCIe/USB power states.
- ⚡ **Balanced / Gaming Mode**: Restores full CPU capabilities, turns on Turbo Boost, and activates high refresh rates.
- ↩ **Safety Restoration**: Automatically backs up original system settings on first run for easy 1-click restore.
- 📊 **Real-time Diagnostics**: Calculates remaining time based on actual WMI power draw (Watts) rather than standard static estimates.
