-- 万象首选候选 LLM 校正：触发后保留 Rime 的原始输入，
-- 在末端 filter 中将经过既有筛选的首选候选交给模型做最小校正。

local wanxiang = require("wanxiang/wanxiang")

local P = {}
local F = {}

local PENDING_PROPERTY = "_wanxiang_llm_correction_pending"
local DEFAULT_TRIGGER = "vv"
local KEY_REPRS = {
    [";"] = "semicolon",
    [","] = "comma",
    ["."] = "period",
    ["/"] = "slash",
    ["'"] = "apostrophe",
    ["-"] = "minus",
    ["="] = "equal",
    ["`"] = "grave",
}

local function trim(text)
    return (text or ""):gsub("^%s*(.-)%s*$", "%1")
end

local function load_config()
    local home = os.getenv("HOME") or os.getenv("USERPROFILE")
    if not home or home == "" then return nil, "HOME/USERPROFILE unavailable" end

    local path = home .. "/.config/rime-llm-translator/config.lua"
    local chunk, load_err = loadfile(path)
    if not chunk then return nil, "config.lua unavailable: " .. tostring(load_err) end

    local ok, config = pcall(chunk)
    if not ok or type(config) ~= "table" then return nil, "invalid config.lua" end
    return config
end

local function get_trigger(config)
    local trigger = config and config.ai_trigger or DEFAULT_TRIGGER
    if type(trigger) ~= "string" or trigger == "" then return DEFAULT_TRIGGER end
    return trigger
end

local function is_trigger_key(key_event, expected)
    local repr = key_event:repr()
    if repr == expected or repr == KEY_REPRS[expected] then return true end

    -- 字母键通常直接返回字面量；少数前端则只提供 ASCII keycode。
    return #expected == 1
        and not key_event:shift()
        and key_event.keycode == expected:byte()
end

local function escape_json(text)
    text = text or ""
    text = text:gsub("\\", "\\\\")
    text = text:gsub('"', '\\"')
    text = text:gsub("\n", "\\n")
    text = text:gsub("\r", "\\r")
    text = text:gsub("\t", "\\t")
    return text
end

local function write_debug_log(record)
    local is_windows = package.config and package.config:sub(1, 1) == "\\"
    local dir = is_windows and (os.getenv("TEMP") or ".") or "/tmp"
    local marker = io.open(dir .. "/.rime_llm_debug_active", "r")
    if not marker then return end
    marker:close()

    local file = io.open(dir .. "/rime_llm_debug.log", "a")
    if not file then return end

    file:write("========== ", os.date("%Y-%m-%d %H:%M:%S"), " [wanxiang_llm_correction] ==========\n")
    file:write("状态: ", record.status or "unknown", "\n")
    file:write("节点模型: ", record.model or "unknown", "\n")
    file:write("原始拼音: ", record.code or "", "\n")
    file:write("输入法首选: ", record.first_text or "", "\n")
    if record.elapsed_seconds ~= nil then
        file:write("请求耗时(秒): ", tostring(record.elapsed_seconds), "\n")
    end
    if record.payload then file:write("请求体（不含 API Key）:\n", record.payload, "\n") end
    if record.response then file:write("响应原文:\n", record.response, "\n") end
    if record.error then file:write("错误: ", record.error, "\n") end
    file:write("=============================================================\n\n")
    file:close()
end

local function json_unescape(text)
    local out = {}
    local i = 1

    while i <= #text do
        local byte = text:byte(i)
        if byte ~= 92 then
            out[#out + 1] = string.char(byte)
            i = i + 1
        else
            local escaped = text:sub(i + 1, i + 1)
            local replacements = {
                ['"'] = '"', ["\\"] = "\\", ["/"] = "/",
                b = "\b", f = "\f", n = "\n", r = "\r", t = "\t",
            }
            if escaped == "u" then
                local hex = text:sub(i + 2, i + 5)
                local cp = tonumber(hex, 16)
                if cp then
                    if cp < 0x80 then
                        out[#out + 1] = string.char(cp)
                    elseif cp < 0x800 then
                        out[#out + 1] = string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
                    else
                        out[#out + 1] = string.char(
                            0xE0 + math.floor(cp / 0x1000),
                            0x80 + math.floor(cp / 0x40) % 0x40,
                            0x80 + cp % 0x40
                        )
                    end
                    i = i + 6
                else
                    return nil
                end
            elseif replacements[escaped] then
                out[#out + 1] = replacements[escaped]
                i = i + 2
            else
                return nil
            end
        end
    end

    return table.concat(out)
end

local function extract_json_string(response, field)
    local _, value_start = response:find('"' .. field .. '"%s*:%s*"')
    if not value_start then return nil end

    local chars = {}
    local i = value_start + 1
    local escaped = false
    while i <= #response do
        local char = response:sub(i, i)
        if escaped then
            chars[#chars + 1] = "\\" .. char
            escaped = false
        elseif char == "\\" then
            escaped = true
        elseif char == '"' then
            return json_unescape(table.concat(chars))
        else
            chars[#chars + 1] = char
        end
        i = i + 1
    end
    return nil
end

local function request_correction(config, original_code, first_text)
    local profile = config.profiles and config.profiles[config.active_profile]
    if type(profile) ~= "table" then
        write_debug_log({ status = "configuration error", code = original_code, first_text = first_text, error = "active profile unavailable" })
        return nil, "active profile unavailable"
    end
    if not profile.api_url or profile.api_url == "" or not profile.api_key or profile.api_key == "" then
        write_debug_log({ status = "configuration error", model = profile.name, code = original_code, first_text = first_text, error = "API configuration incomplete" })
        return nil, "API configuration incomplete"
    end

    local prompt = [[
{"role":"逆向拼音纠错专家","protocol":"仅输出修正后汉字,严禁解释多言,长度严格守恒","logic":"1.声学还原:兼容平翘舌、前后鼻音、fh模糊音;2.意境采样:文学语境(含山/巅/长风/明月/负/至)史诗感优先,即便原句通顺,若同音字张力更强(如负变扶)则强制替换；上下文原则：用户携带历史输入时,必须按照历史输入的信息推断预测内容,例如人称性别区分","output":"直接输出修正后的文本"}

执行规则：
1. 用户主动触发了纠错，因此输入法首选确定存在至少一处错误，不能原样返回。
2. 以输入法首选为草稿，结合中文语义、固定搭配、语法和上下文，修复同音字误选、错别字、分词和标点。
3. 兼容平翘舌、前后鼻音、fh 模糊音等可能造成的同音误选；文学语境优先保留更有张力、更符合意境的用字（例如“负”可改为“扶”）。
4. 如果请求中提供了历史输入，必须利用它判断上下文、人称和性别；没有提供时不得臆造历史信息。
5. 输出长度严格守恒：不得增字、删字或改动英文/数字的数量；只替换必要的汉字或标点。
6. 即使原句表面通顺，也必须检查并替换至少一处最可疑的字；不得进行与纠错无关的润色、扩写或改写。

只输出修正后的完整文本。禁止输出分析、理由、引号、Markdown、JSON、标签或任何前后缀。
]]
    local user_content = "【输入法首选】" .. first_text

    local lower_url = string.lower(profile.api_url)
    local lower_model = string.lower(profile.model or "")
    local is_anthropic = lower_url:find("anthropic", 1, true) or lower_url:find("messages", 1, true)
    local is_deepseek = (lower_url:find("deepseek", 1, true) or lower_model:find("deepseek", 1, true))
        and not lower_model:find("chat", 1, true)
    local is_gemini = lower_url:find("generativelanguage", 1, true) or lower_model:find("gemini", 1, true)
    local is_mimo = (lower_url:find("xiaomimimo", 1, true) or lower_model:find("mimo", 1, true))
        and not lower_model:find("tts", 1, true)
    local runtime_model = profile.model or ""
    local thinking_json = ""
    if is_deepseek then
        runtime_model = runtime_model:gsub("deepseek%-reasoner", "deepseek-chat")
        thinking_json = ',"thinking":{"type":"disabled"}'
    elseif is_mimo then
        thinking_json = ',"thinking":{"type":"disabled"}'
    elseif is_gemini then
        -- Gemini OpenAI 兼容端不支持关闭值；low 是其最低推理强度。
        thinking_json = ',"reasoning_effort":"low"'
    end

    -- 校正候选只需一句输出，避免沿用整句生成器的 4000 token 预算。
    local max_tokens = tonumber(config.max_tokens) or 4000
    max_tokens = math.max(32, math.min(max_tokens, 256))
    local payload
    if is_anthropic then
        payload = string.format(
            '{"model":"%s","system":"%s","messages":[{"role":"user","content":"%s"}],"temperature":%s,"max_tokens":%s}',
            escape_json(runtime_model),
            escape_json(prompt),
            escape_json(user_content),
            tostring(config.temperature or 0.1),
            tostring(max_tokens)
        )
    else
        payload = string.format(
            '{"model":"%s","messages":[{"role":"system","content":"%s"},{"role":"user","content":"%s"}],"temperature":%s,"max_tokens":%s%s}',
            escape_json(runtime_model),
            escape_json(prompt),
            escape_json(user_content),
            tostring(config.temperature or 0.1),
            tostring(max_tokens),
            thinking_json
        )
    end

    local payload_path = os.tmpname()
    local file = io.open(payload_path, "wb")
    if not file then
        write_debug_log({ status = "request error", model = profile.name, code = original_code, first_text = first_text, payload = payload, error = "cannot create request payload" })
        return nil, "cannot create request payload"
    end
    file:write(payload)
    file:close()

    local is_windows = package.config and package.config:sub(1, 1) == "\\"
    local curl
    if is_windows then
        local url = profile.api_url:gsub('"', '\\"')
        local key = profile.api_key:gsub('"', '\\"')
        local headers
        if is_anthropic then
            headers = string.format('-H "x-api-key: %s" -H "anthropic-version: 2023-06-01"', key)
        else
            headers = string.format('-H "Authorization: Bearer %s"', key)
        end
        curl = string.format(
            'curl.exe -fsSL --retry 2 --retry-all-errors --connect-timeout %s --max-time %s -X POST "%s" -H "Content-Type: application/json" %s --data-binary "@%s" 2>&1',
            tostring(config.connect_timeout or 2.0),
            tostring(config.max_time or 30.0),
            url,
            headers,
            payload_path
        )
    else
        local safe_payload = payload:gsub("'", "'\\''")
        local safe_url = profile.api_url:gsub("'", "'\\''")
        local safe_key = profile.api_key:gsub("'", "'\\''")
        local headers
        if is_anthropic then
            headers = string.format("-H 'x-api-key: %s' -H 'anthropic-version: 2023-06-01'", safe_key)
        else
            headers = string.format("-H 'Authorization: Bearer %s'", safe_key)
        end
        curl = string.format(
            "curl -fsSL --retry 2 --retry-all-errors --connect-timeout %s --max-time %s -X POST '%s' -H 'Content-Type: application/json' %s -d '%s' 2>&1",
            tostring(config.connect_timeout or 2.0),
            tostring(config.max_time or 30.0),
            safe_url,
            headers,
            safe_payload
        )
    end

    local started_at = os.time()
    write_debug_log({
        status = "request started",
        model = profile.name,
        code = original_code,
        first_text = first_text,
        payload = payload,
    })
    local handle = io.popen(curl)
    if not handle then
        os.remove(payload_path)
        write_debug_log({ status = "request error", model = profile.name, code = original_code, first_text = first_text, payload = payload, error = "cannot start curl" })
        return nil, "cannot start curl"
    end
    local response = handle:read("*a")
    local close_ok = handle:close()
    os.remove(payload_path)

    local field = is_anthropic and "text" or "content"
    local corrected = response and extract_json_string(response, field) or nil
    local elapsed_seconds = os.time() - started_at
    if not corrected then
        write_debug_log({
            status = close_ok and "response error" or "curl error",
            model = profile.name,
            code = original_code,
            first_text = first_text,
            elapsed_seconds = elapsed_seconds,
            payload = payload,
            response = response,
            error = "response did not contain text",
        })
        return nil, "response did not contain text"
    end

    corrected = trim(corrected:gsub("<think>.-</think>", ""):gsub("<think>.*", ""))
    if corrected == "" then
        write_debug_log({
            status = "response error",
            model = profile.name,
            code = original_code,
            first_text = first_text,
            elapsed_seconds = elapsed_seconds,
            payload = payload,
            response = response,
            error = "empty correction",
        })
        return nil, "empty correction"
    end
    write_debug_log({
        status = "success",
        model = profile.name,
        code = original_code,
        first_text = first_text,
        elapsed_seconds = elapsed_seconds,
        payload = payload,
        response = response,
    })
    return corrected, nil, profile.name or "AI"
end

local function pass_through(first, next_candidate, state)
    if first then yield(first) end
    while true do
        local candidate = next_candidate(state)
        if not candidate then return end
        yield(candidate)
    end
end

function P.init(env)
    local config = load_config()
    env.trigger = get_trigger(config)
end

function P.fini(env)
    env.trigger = nil
end

function P.func(key_event, env)
    if key_event:release() then return wanxiang.RIME_PROCESS_RESULTS.kNoop end

    local trigger = env.trigger or DEFAULT_TRIGGER
    local prefix = trigger:sub(1, -2)
    local final_char = trigger:sub(-1)
    if not is_trigger_key(key_event, final_char) then
        return wanxiang.RIME_PROCESS_RESULTS.kNoop
    end

    local context = env.engine.context
    local input = context.input or ""
    if context.caret_pos ~= #input or input == "" then
        return wanxiang.RIME_PROCESS_RESULTS.kNoop
    end
    if prefix ~= "" and (#input <= #prefix or input:sub(-#prefix) ~= prefix) then
        return wanxiang.RIME_PROCESS_RESULTS.kNoop
    end

    local composition = context.composition
    local segment = composition and not composition:empty() and composition:back()
    if not segment or not (segment:has_tag("abc") or segment:has_tag("llm_pinyin")) then
        return wanxiang.RIME_PROCESS_RESULTS.kNoop
    end

    if prefix ~= "" and not context:pop_input(#prefix) then
        return wanxiang.RIME_PROCESS_RESULTS.kNoop
    end
    context:set_property(PENDING_PROPERTY, "1")
    context:refresh_non_confirmed_composition()
    return wanxiang.RIME_PROCESS_RESULTS.kAccepted
end

function F.func(input, env)
    local context = env.engine.context
    if context:get_property(PENDING_PROPERTY) ~= "1" then
        for candidate in input:iter() do yield(candidate) end
        return
    end

    -- 先清除标记，避免 refresh 或异常路径触发重复请求。
    context:set_property(PENDING_PROPERTY, "")

    local next_candidate, state = input:iter()
    local first = next_candidate(state)
    if not first then return end

    local code = context.input or ""
    if code == "" or first.start ~= 0 or first._end ~= #code then
        pass_through(first, next_candidate, state)
        return
    end

    local first_text = trim(first.text)
    if first_text == "" then
        pass_through(first, next_candidate, state)
        return
    end

    local config, config_error = load_config()
    if not config then
        write_debug_log({
            status = "configuration error",
            code = code,
            first_text = first_text,
            error = config_error or "config.lua unavailable",
        })
        pass_through(first, next_candidate, state)
        return
    end

    local corrected, _, model_name = request_correction(config, code, first_text)
    if corrected and corrected ~= first_text then
        yield(Candidate("llm_correction", first.start, first._end, corrected, "✨ " .. model_name .. "·纠错"))
    end
    pass_through(first, next_candidate, state)
end

return { P = P, F = F }
