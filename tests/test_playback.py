import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import core
import playback

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


class PlaybackTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("mpv"), "mpv is needed to verify script-option parsing")
    def test_real_mpv_receives_separate_monitor_options_and_user_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "mpv.conf").write_text("vo=null\nao=null\nvolume=37\nwindow-maximized=yes\n")
            script = root / "check.lua"
            script.write_text('''local mp=require "mp"
mp.add_timeout(0.1,function()
    local valid=mp.get_opt("yt_inbox-mode")=="auto" and mp.get_opt("yt_inbox-online")=="yes"
        and mp.get_opt("yt_inbox-stage")=="2" and mp.get_opt("yt_inbox-format2"):find("height<=720",1,true)
        and mp.get_property_number("volume")==37 and mp.get_property_bool("window-maximized")
    mp.commandv("quit",valid and "0" or "1")
end)
''')
            args = playback.arguments("unused", "auto", height=2160, stage=2)[:-2]
            result = subprocess.run([shutil.which("mpv"), *args, "--idle=yes", "--script=" + str(script)],
                                    env=dict(os.environ, MPV_HOME=folder), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_legacy_settings_default_to_auto_and_validate_modes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = core.Paths(root / "config", root / "data", root / "state")
            paths.ensure()
            self.assertEqual(core.settings(paths).get("playback_mode", playback.DEFAULT_MODE), "auto")
            for value in ("auto", "low", "balanced", "high", "custom"):
                values = core.settings(paths)
                values["playback_mode"] = value
                core.save_settings(paths, values)
                self.assertEqual(core.settings(paths)["playback_mode"], value)
            for invalid in (None, [], True, "turbo"):
                values["playback_mode"] = invalid
                core.save_settings(paths, values)
                with self.assertRaises(core.InboxError):
                    core.settings(paths)

    def test_screen_tiers_and_unknown_screen_are_bounded(self):
        for height, expected in ((768, 720), (1080, 1080), (1440, 1440), (2160, 2160),
                                 (4320, 2160), (None, 1080), (float("nan"), 1080)):
            self.assertEqual(playback.screen_limit(height), expected)

    def test_custom_config_and_offline_file_keep_distinct_behaviour(self):
        custom = playback.arguments("https://www.youtube.com/watch?v=" + "a" * 11, "custom")
        self.assertFalse(any(arg.startswith(("--hwdec=", "--ytdl-format=", "--no-config", "--config-dir")) for arg in custom))
        local = playback.arguments("/tmp/video with spaces.mkv", "auto", local=True)
        self.assertIn("--hwdec=auto", local)
        self.assertIn("--ytdl=no", local)
        self.assertFalse(any(arg.startswith("--ytdl-format=") for arg in local))
        self.assertEqual(local[-2:], ["--", "/tmp/video with spaces.mkv"])

    def test_status_protocol_handles_split_messages_malformed_data_and_bounded_buffer(self):
        data = {"height": 1080, "codec": "vp9", "hwdec": "vaapi", "online": True}
        encoded = (playback.MARKER + json.dumps(data) + "\n").encode()
        pending, messages, log = playback.read_messages(b"", b"warning\n" + encoded[:20])
        self.assertEqual(log, "warning")
        self.assertFalse(messages)
        pending, messages, log = playback.read_messages(pending, encoded[20:] + b"YT_INBOX_PLAYBACK:{bad}\n")
        self.assertEqual(messages, [data])
        self.assertIn("hardware decoding (vaapi)", playback.status_message(data))
        self.assertIn("software decoding", playback.status_message(dict(data, hwdec="no")))
        self.assertIn("unconfirmed", playback.status_message(dict(data, hwdec=None)))
        self.assertEqual(playback.status_message(dict(data, height=float("nan"))), "")
        self.assertLessEqual(len(playback.read_messages(b"", b"a" * 50000)[0]), 16384)


@unittest.skipUnless(yt_dlp, "yt-dlp is needed to verify real format selection")
class FormatSelectionTests(unittest.TestCase):
    def formats(self):
        rows = []
        for name, height, fps, codec in (("avc720", 720, 30, "avc1.64001f"),
                ("avc1080", 1080, 30, "avc1.640028"), ("vp1080", 1080, 60, "vp9"),
                ("vp2160", 2160, 60, "vp9"), ("av2160", 2160, 60, "av01.0.12M.08"),
                ("av4320", 4320, 60, "av01.0.16M.08")):
            rows.append({"format_id": name, "url": "https://example.com/" + name,
                         "height": height, "fps": fps, "vcodec": codec, "acodec": "none", "ext": "mp4"})
        rows.append({"format_id": "audio", "url": "https://example.com/audio", "vcodec": "none", "acodec": "opus", "ext": "webm"})
        return rows

    def select(self, selection, rows=None):
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format": selection}) as ydl:
            return ydl.process_ie_result({"id": "fixture", "title": "Fixture", "formats": rows or self.formats()}, download=False)

    def test_auto_respects_small_and_large_displays_and_excludes_av1(self):
        for height, expected in ((768, "avc720"), (1080, "vp1080"), (2160, "vp2160")):
            selected = self.select(playback.auto_formats(height)[0])
            self.assertEqual(selected["requested_formats"][0]["format_id"], expected)

    def test_low_power_prefers_h264_and_high_quality_allows_4k_av1(self):
        for mode, expected in (("low", "avc720"), ("balanced", "vp1080"), ("high", "av2160")):
            args = playback.arguments("https://example.com/", mode)
            selector = next(a.split("=", 1)[1] for a in args if a.startswith("--ytdl-format="))
            self.assertEqual(self.select(selector)["requested_formats"][0]["format_id"], expected)

    def test_new_offline_auto_copy_prefers_h264_without_exceeding_download_quality(self):
        selected = self.select(playback.download_format(1080))
        self.assertEqual(selected["requested_formats"][0]["format_id"], "avc1080")
        # H.264 absent: fall back to VP9, never silently to AV1 or unrestricted 4K.
        rows = [f for f in self.formats() if not f["format_id"].startswith("avc")]
        self.assertEqual(self.select(playback.download_format(1080), rows)["requested_formats"][0]["format_id"], "vp1080")


LUA = next((ctypes.util.find_library(name) for name in ("lua5.4", "lua5.3", "lua5.2") if ctypes.util.find_library(name)), None)


@unittest.skipUnless(LUA, "a Lua 5.2+ shared library is needed for adaptation scenarios")
class AdaptationTests(unittest.TestCase):
    def run_scenario(self, scenario, mode="auto", online="yes"):
        lib = ctypes.CDLL(LUA)
        lib.luaL_newstate.restype = ctypes.c_void_p
        lib.luaL_openlibs.argtypes = [ctypes.c_void_p]
        lib.luaL_loadbufferx.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_char_p]
        lib.lua_pcallk.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ssize_t, ctypes.c_void_p]
        lib.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.lua_tolstring.restype = ctypes.c_char_p
        lib.lua_close.argtypes = [ctypes.c_void_p]
        script = Path(playback.__file__).with_name("playback.lua").read_text()
        harness = r'''
clock=100; events={}; hooks={}; commands={}; output={}; settings={}
props={height=2160, ["video-format"]="vp9", ["hwdec-current"]="vaapi", ["time-pos"]=100,
    duration=600, seekable=true, path="https://www.youtube.com/watch?v=aaaaaaaaaaa", ["estimated-vf-fps"]=30}
local function get(key, default) if props[key] ~= nil then return props[key] end return default end
local fake={get_time=function() return clock end, get_property=get, get_property_number=get, get_property_bool=get,
    set_property=function(key,value) settings[key]=value end, set_property_bool=function(key,value) settings[key]=value end,
    add_hook=function(name,priority,fn) hooks[name]=fn end,
    register_event=function(name,fn) events[name]=fn end,
    add_periodic_timer=function(interval,fn) tick_fn=fn end,
    commandv=function(...) table.insert(commands,{...}) end}
package.preload["mp"]=function() return fake end
package.preload["mp.options"]=function() return {read_options=function(opts)
    opts.mode=MODE; opts.online=ONLINE
    for i=0,3 do opts["format"..i]="format"..i end
end} end
package.preload["mp.utils"]=function() return {format_json=function(data) table.insert(output,data); return "{}" end} end
io.stdout={write=function() end,flush=function() end}
function tick(seconds,changes)
    clock=clock+seconds; props["time-pos"]=props["time-pos"]+seconds
    for key,value in pairs(changes or {}) do props[key]=value end
    tick_fn()
end
function loaded() hooks.on_load(); events["file-loaded"]() end
'''
        source = ("MODE=" + json.dumps(mode) + "; ONLINE=" + json.dumps(online) + "\n" + harness + "\n" + script + "\nloaded()\n" + scenario).encode()
        state = lib.luaL_newstate()
        try:
            lib.luaL_openlibs(state)
            status = lib.luaL_loadbufferx(state, source, len(source), b"playback-scenario", None)
            if not status:
                status = lib.lua_pcallk(state, 0, 0, 0, 0, None)
            self.assertEqual(status, 0, lib.lua_tolstring(state, -1, None).decode() if status else "")
        finally:
            lib.lua_close(state)

    def test_hardware_accelerated_4k_stays_at_selected_quality(self):
        self.run_scenario('for i=1,40 do tick(2) end; assert(#commands==0); assert(output[#output].hwdec=="vaapi")')

    def test_software_fallback_preserves_position_and_stops_at_720p(self):
        self.run_scenario('''
tick(4,{["hwdec-current"]="no",["time-pos"]=123.25})
assert(#commands==1); assert(commands[1][1]=="loadfile"); assert(commands[1][2]==props.path)
loaded(); assert(settings["file-local-options/start"]=="123.25")
assert(settings["file-local-options/pause"]==false)
assert(settings["file-local-options/ytdl-format"]=="format1")
tick(4,{height=1080,["video-format"]="h264"})
assert(#commands==2); loaded()
for i=1,40 do tick(2,{height=720}) end
assert(#commands==2); assert(output[#output].stage==2)
''')

    def test_pause_seek_buffer_unknown_duration_and_unknown_decoder_do_not_restart(self):
        conditions = ["props.pause=true", 'props["paused-for-cache"]=true', 'props.seeking=true',
                      'props["eof-reached"]=true', 'props.speed=2', 'props.seekable=false',
                      'props.duration=nil', 'props["hwdec-current"]="unknown"',
                      'props.path="https://example.com/different-video"']
        for condition in conditions:
            with self.subTest(condition=condition):
                self.run_scenario('props["hwdec-current"]="no"; ' + condition + '; for i=1,20 do tick(2) end; assert(#commands==0)')
        self.run_scenario('props["hwdec-current"]="no"; tick(2); events.seek(); tick(2); assert(#commands==0)')

    def test_only_sustained_drops_cause_bounded_step_downs(self):
        self.run_scenario('''
tick(4); tick(2,{["frame-drop-count"]=30}); tick(2); tick(2); assert(#commands==0)
for level=1,3 do
    for i=1,6 do tick(2,{["frame-drop-count"]=(props["frame-drop-count"] or 0)+20}) end
    assert(#commands==level); loaded()
end
for i=1,20 do tick(2,{["frame-drop-count"]=(props["frame-drop-count"] or 0)+20}) end
assert(#commands==3); assert(output[#output].stage==3)
''')

    def test_offline_and_explicit_modes_never_reload_the_video(self):
        scenario = 'props["hwdec-current"]="no"; for i=1,20 do tick(2,{["frame-drop-count"]=i*40}) end; assert(#commands==0)'
        for mode in ("low", "balanced", "high", "custom"):
            self.run_scenario(scenario, mode=mode)
        self.run_scenario(scenario, online="no")


if __name__ == "__main__":
    unittest.main()
