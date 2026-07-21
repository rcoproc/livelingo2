"""
list_devices.py
===============
Utility: list every audio device sounddevice can see, with its index, so you
can fill in INPUT_DEVICE / OUTPUT_DEVICE in your .env or config.py.

Run:
    python list_devices.py

Look for:
  * your microphone in the INPUT column, and
  * "CABLE Input (VB-Audio Virtual Cable)" in the OUTPUT column
    (that is the device this tool should send translated audio to).
"""

from colorama import Fore, Style, init

from livelingo import devices

init(autoreset=True)


def main():
    rows = devices.summary_rows()
    default_in = devices.default_input_index()
    default_out = devices.default_output_index()

    print(Fore.CYAN + Style.BRIGHT + "\nAvailable audio devices")
    print(Fore.CYAN + "=" * 78 + Style.RESET_ALL)
    print(
        Style.BRIGHT
        + f"{'idx':>3}  {'in':>2} {'out':>3}  {'host API':<14} name"
        + Style.RESET_ALL
    )
    print("-" * 78)

    for idx, name, in_ch, out_ch, hostapi in rows:
        # Color: green if it can record (mic), magenta if it can play (output).
        if in_ch > 0 and out_ch == 0:
            color = Fore.GREEN
        elif out_ch > 0 and in_ch == 0:
            color = Fore.MAGENTA
        else:
            color = Fore.WHITE

        tags = []
        if idx == default_in:
            tags.append("default-in")
        if idx == default_out:
            tags.append("default-out")
        if "cable" in name.lower():
            tags.append("VB-CABLE")
        tag_str = ("  <- " + ", ".join(tags)) if tags else ""

        print(
            color
            + f"{idx:>3}  {in_ch:>2} {out_ch:>3}  {hostapi:<14} {name}"
            + Style.BRIGHT
            + Fore.YELLOW
            + tag_str
        )

    print("-" * 78)
    print(
        Fore.GREEN + "green = input (microphone)   "
        + Fore.MAGENTA + "magenta = output (playback)   "
        + Fore.WHITE + "white = both"
    )

    vb_idx, vb_name = devices.find_vbcable_output()
    print()
    if vb_idx is not None:
        print(
            Fore.GREEN
            + Style.BRIGHT
            + f"[ok] VB-Cable detected at index {vb_idx}: {vb_name}"
        )
        print(
            Style.RESET_ALL
            + "     Set OUTPUT_DEVICE to this index (or leave the default "
            '"CABLE Input").'
        )
    else:
        print(
            Fore.YELLOW
            + Style.BRIGHT
            + "[!] VB-Cable was NOT found. Install it from https://vb-audio.com/Cable/"
        )
    print()


if __name__ == "__main__":
    main()
