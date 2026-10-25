-- Observe the real decoder. Auto only steps down, and never changes watch status.
local mp = require "mp"
local options = require "mp.options"
local utils = require "mp.utils"
local opts = {mode="custom", online="no", stage=0,
              format0="", format1="", format2="", format3=""}
options.read_options(opts, "yt_inbox")

local stage = math.min(3, math.max(0, tonumber(opts.stage) or 0))
local target, resume, loaded_at, previous, bad_seconds = nil, nil, nil, nil, 0
local last_seek = 0
local changing = false

local function emit(data)
    data.stage = stage
    data.online = opts.online == "yes"
    io.stdout:write("YT_INBOX_PLAYBACK:" .. utils.format_json(data) .. "\n")
    io.stdout:flush()
end

local function automatic()
    return opts.mode == "auto" and opts.online == "yes"
end

mp.add_hook("on_load", 5, function()
    local path = mp.get_property("path")
    if not target then target = path end
    if automatic() and path == target then
        mp.set_property("file-local-options/ytdl-format", opts["format" .. stage])
    end
    if resume and path == target then
        mp.set_property("file-local-options/start", tostring(resume.position))
        mp.set_property_bool("file-local-options/pause", resume.paused)
        resume = nil
    end
end)

mp.register_event("file-loaded", function()
    loaded_at = mp.get_time()
    previous, bad_seconds, changing = nil, 0, false
end)
mp.register_event("end-file", function()
    loaded_at, previous, bad_seconds = nil, nil, 0
end)
mp.register_event("seek", function()
    last_seek = mp.get_time()
    previous, bad_seconds = nil, 0
end)

local function lower(next_stage, position)
    next_stage = math.min(3, next_stage)
    if next_stage <= stage or changing or opts["format" .. next_stage] == "" then return end
    stage, changing = next_stage, true
    resume = {position=position, paused=mp.get_property_bool("pause", false)}
    emit({event="adjusting"})
    -- Reload inside the same mpv window; the early hook restores the position.
    mp.commandv("loadfile", target, "replace")
end

mp.add_periodic_timer(2, function()
    if not loaded_at or changing then return end
    local now = mp.get_time()
    local height = mp.get_property_number("height", 0)
    local codec = mp.get_property("video-format", "unknown")
    local hwdec = mp.get_property("hwdec-current", "unknown")
    local pos = mp.get_property_number("time-pos")
    emit({event="status", height=height, codec=codec, hwdec=hwdec})
    if not automatic() or stage >= 3 or mp.get_property("path") ~= target then return end
    -- Skip non-seekable/unknown-duration streams, pauses, seeks and buffering.
    if not pos or height <= 0 or not mp.get_property_bool("seekable", false)
        or not mp.get_property_number("duration")
        or mp.get_property_bool("pause", false)
        or mp.get_property_bool("paused-for-cache", false)
        or mp.get_property_bool("seeking", false)
        or mp.get_property_bool("eof-reached", false)
        or math.abs(mp.get_property_number("speed", 1) - 1) > 0.01
        or now - loaded_at < 4 or now - last_seek < 4 then
        previous, bad_seconds = nil, 0
        return
    end
    -- H.264 at <=720p is the conservative software fallback. Unknown decoder
    -- state is not evidence of software decoding and must not trigger a retry.
    if hwdec == "no" then
        if stage == 0 and codec ~= "h264" then lower(1, pos); return end
        if stage < 2 and height > 720 then lower(2, pos); return end
    end
    local drops = mp.get_property_number("frame-drop-count", 0)
                + mp.get_property_number("decoder-frame-drop-count", 0)
    local fps = mp.get_property_number("estimated-vf-fps", 0)
    if previous and fps > 0 then
        local elapsed, delta = now - previous.time, drops - previous.drops
        local progress = pos - previous.position
        if elapsed > 0 and elapsed <= 5 and progress > 0 and progress <= elapsed + 1
            and delta >= 0 and delta >= math.max(3, fps * elapsed * 0.05) then
            bad_seconds = bad_seconds + elapsed
        else
            bad_seconds = 0
        end
        if bad_seconds >= 8 then lower(stage + 1, pos); return end
    end
    previous = {time=now, drops=drops, position=pos}
end)
