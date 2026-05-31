#!/usr/bin/env python3
"""Minimal Standard MIDI File parser -> note events, for inspecting the Kayser MIDI."""
import sys, struct

def read_vlq(data, i):
    val = 0
    while True:
        b = data[i]; i += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return val, i

def parse(path):
    with open(path, "rb") as f:
        data = f.read()
    assert data[:4] == b"MThd"
    hlen = struct.unpack(">I", data[4:8])[0]
    fmt, ntrks, division = struct.unpack(">HHH", data[8:8+hlen])
    print(f"format={fmt} ntrks={ntrks} division={division}")
    i = 8 + hlen
    tracks = []
    while i < len(data):
        if data[i:i+4] != b"MTrk":
            break
        tlen = struct.unpack(">I", data[i+4:i+8])[0]
        track = data[i+8:i+8+tlen]
        tracks.append(track)
        i += 8 + tlen

    all_notes = []  # (abs_tick, track_idx, note, velocity, on/off)
    for ti, track in enumerate(tracks):
        i = 0; tick = 0; running = None
        while i < len(track):
            dt, i = read_vlq(track, i)
            tick += dt
            status = track[i]
            if status & 0x80:
                running = status; i += 1
            else:
                status = running
            if status == 0xFF:  # meta
                mtype = track[i]; i += 1
                mlen, i = read_vlq(track, i)
                i += mlen
            elif status in (0xF0, 0xF7):  # sysex
                slen, i = read_vlq(track, i)
                i += slen
            else:
                hi = status & 0xF0
                if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                    d1 = track[i]; d2 = track[i+1]; i += 2
                    if hi == 0x90 and d2 > 0:
                        all_notes.append((tick, ti, d1, d2, "on"))
                    elif hi == 0x80 or (hi == 0x90 and d2 == 0):
                        all_notes.append((tick, ti, d1, d2, "off"))
                elif hi in (0xC0, 0xD0):
                    i += 1
                else:
                    i += 1
    return division, all_notes

NAMES = ["C","C#","D","D#","E","F","F#","G","G#","A","A#","B"]
def name(n):
    return f"{NAMES[n%12]}{n//12-1}"

if __name__ == "__main__":
    div, notes = parse(sys.argv[1])
    ons = [n for n in notes if n[4]=="on"]
    ons.sort()
    print("total note-ons:", len(ons))
    print("ticks/quarter:", div)
    # print as (tick, beat_in_quarters, pitch)
    for tick, ti, p, v, _ in ons:
        print(f"{tick:7d}  q={tick/div:8.3f}  trk{ti:<2d}  {name(p):4s} vel{v}")
