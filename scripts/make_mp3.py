"""
OPIc 스크립트(.md) -> 영어 MP3 자동 변환기 (Microsoft Edge TTS 기반)

사용법:
  # 기본 모드 (전체 스크립트를 mp3/ 폴더에 자연스러운 속도로 변환)
  python make_mp3.py

  # 쉐도잉 모드 (문장 사이 2.5초 공백, 0.9배속)
  python make_mp3.py --mode shadowing --pause 2.5 --rate=-10%

  # 문장 2번 반복 쉐도잉 모드
  python make_mp3.py --mode shadowing --repeat 2 --pause 2.0

  # 특정 파일만 변환
  python make_mp3.py 03.공원가기.md

  # 추출된 영어 텍스트만 미리보기 (네트워크 불필요)
  python make_mp3.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

# 파일 번호별 영어 토픽 안내 음성 (운전 중 어떤 주제인지 바로 알 수 있게 함)
TOPIC_TITLES: dict[str, str] = {
    "00": "Essential Filler Expressions.",
    "01": "Topic 1. Home.",
    "02": "Topic 2. Concerts.",
    "03": "Topic 3. Going to the Park.",
    "04": "Topic 4. Domestic Travel.",
    "05": "Topic 5. Overseas Travel.",
    "06": "Topic 6. Listening to Music.",
    "07": "Topic 7. Gatherings.",
    "08": "Topic 8. Hair Salon.",
    "09": "Topic 9. Movies.",
    "10": "Topic 10. Weather.",
    "11": "Topic 11. Transportation.",
    "12": "Topic 12. Recycling.",
    "13": "Topic 13. Geography.",
    "14": "Topic 14. Fashion.",
}

# 영어 문장 안에 섞인 한국어 지명 치환
PLACE_NAME_MAP: dict[str, str] = {
    "속초": "Sokcho",
    "용인": "Yongin",
    "서울": "Seoul",
    "수원": "Suwon",
    "한강": "the Han River",
}

# 롤플레이 영어 지문(질문) 필터 패턴
PROMPT_LINE_PATTERNS = (
    re.compile(r"^You are planning\b", re.IGNORECASE),
    re.compile(r"^You and your friend are planning\b", re.IGNORECASE),
    re.compile(r"^You want to rent\b", re.IGNORECASE),
    re.compile(r"^Explain the situation\b", re.IGNORECASE),
    re.compile(r'^"?When people plan to travel\b', re.IGNORECASE),
    re.compile(r'^"?Have you ever experienced\b', re.IGNORECASE),
    re.compile(r"^https?://", re.IGNORECASE),
)

HANGUL_RE = re.compile(r"[가-힣]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")


def is_english_dominant(text: str) -> bool:
    en = len(ASCII_LETTER_RE.findall(text))
    ko = len(HANGUL_RE.findall(text))
    return en > 0 and en > ko * 2


def clean_line(line: str) -> str:
    text = line.strip()
    # 마크다운 서식 제거
    text = re.sub(r"[*_`]+", "", text)
    # 스마트 따옴표 정규화
    text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    # 문장 전체를 감싼 따옴표 제거
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    # 다중 공백 정리
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_filler_sections(content: str) -> list[list[str]]:
    """00.filler_readable.md에서 백틱/따옴표 영어 표현만 깔끔하게 추출"""
    sections: list[list[str]] = []
    current: list[str] = []

    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("### ") or line.startswith("## "):
            if current:
                sections.append(current)
                current = []
            continue

        # * **`Oh, that's a great question!`** 형태
        m = re.search(r"\*\*`([^`]+)`\*\*", line)
        if m:
            phrase = clean_line(m.group(1))
            if phrase and not HANGUL_RE.search(phrase):
                current.append(phrase)
            continue

        # * 문장: *"To be honest, ..."* 형태
        m2 = re.search(r'문장:\s*\*"([^"]+)"\*', line)
        if m2:
            phrase = clean_line(m2.group(1))
            if phrase and not HANGUL_RE.search(phrase):
                current.append(phrase)

    if current:
        sections.append(current)
    return sections


def extract_topic_sections(content: str) -> list[list[str]]:
    """일반 주제 스크립트(.md)에서 순수 영어 답변 섹션만 추출"""
    sections: list[list[str]] = []
    current: list[str] = []

    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue

        # 섹션 헤더나 한국어 줄을 만나면 섹션 구분
        if stripped.startswith("#") or re.match(r"^\d+[\.\-]", stripped):
            if current:
                sections.append(current)
                current = []
            continue

        # 영어 우세 줄에 포함된 지명(속초, 용인 등)을 영어로 치환
        candidate = stripped
        if is_english_dominant(candidate):
            for ko, en in PLACE_NAME_MAP.items():
                candidate = candidate.replace(ko, en)

        # 한국어가 남아있으면 한국어 메모/번역 줄로 판단하고 스킵
        if HANGUL_RE.search(candidate):
            if current:
                sections.append(current)
                current = []
            continue

        # URL / 롤플레이 지시문 스킵
        if any(pat.search(candidate) for pat in PROMPT_LINE_PATTERNS):
            if current:
                sections.append(current)
                current = []
            continue

        cleaned = clean_line(candidate)
        if len(ASCII_LETTER_RE.findall(cleaned)) >= 3:
            current.append(cleaned)

    if current:
        sections.append(current)
    return sections


def split_sentences(paragraph: str) -> list[str]:
    """문단 텍스트를 쉐도잉용 문장 단위로 분리"""
    # Mr., Ms., Dr., Mt. 같은 약어 뒤 마침표 보호
    protected = re.sub(r"\b(Mr|Ms|Mrs|Dr|Mt|St)\.", r"\1<DOT>", paragraph)
    parts = re.split(r"(?<=[.!?])\s+", protected)
    return [p.replace("<DOT>", ".").strip() for p in parts if p.strip()]


def make_silence_mp3(duration_sec: float, reference_mp3: bytes) -> bytes:
    """
    ffmpeg 없이 edge-tts MP3 프레임 헤더와 동일한 규격의 무음 MP3 바이트 생성.
    edge-tts 기본 포맷: MPEG-2 Layer III, 24kHz, 48kbps, Mono (144 bytes / 24ms)
    """
    if duration_sec <= 0:
        return b""

    # reference_mp3에서 첫 번째 유효 MP3 동기화 프레임 헤더 탐색
    frame_size = 144
    frame_ms = 24.0
    header = bytes([0xFF, 0xF3, 0x64, 0xC4])  # MPEG2 L3, 48kbps, 24kHz, Mono, no-padding

    bitrates_mpeg1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
    bitrates_mpeg2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
    sr_table = {
        3: [44100, 48000, 32000],  # MPEG 1
        2: [22050, 24000, 16000],  # MPEG 2
        0: [11025, 12000, 8000],   # MPEG 2.5
    }

    for i in range(len(reference_mp3) - 4):
        b0, b1, b2, b3 = reference_mp3[i : i + 4]
        if b0 == 0xFF and (b1 & 0xE0) == 0xE0:
            version_id = (b1 >> 3) & 0x03
            layer_id = (b1 >> 1) & 0x03
            br_idx = (b2 >> 4) & 0x0F
            sr_idx = (b2 >> 2) & 0x03
            if version_id in sr_table and layer_id == 1 and 0 < br_idx < 15 and sr_idx < 3:
                sr = sr_table[version_id][sr_idx]
                br = (bitrates_mpeg1 if version_id == 3 else bitrates_mpeg2)[br_idx] * 1000
                samples = 1152 if version_id == 3 else 576
                frame_size = (samples // 8) * br // sr
                frame_ms = (samples / sr) * 1000.0
                # padding bit(bit 1 of b2)와 CRC bit(bit 0 of b1 -> 1 means no CRC) 정규화
                header = bytes([b0, b1 | 0x01, b2 & 0xFD, b3])
                break

    silent_frame = header + b"\x00" * (frame_size - 4)
    num_frames = max(1, round((duration_sec * 1000.0) / frame_ms))
    return silent_frame * num_frames


def sanitize_for_tts(text: str) -> str:
    """TTS 엔진이 안정적으로 읽도록 특수기호 정리"""
    text = text.replace("—", ", ").replace("–", ", ").replace("…", ".")
    text = re.sub(r"\.{2,}", ".", text)
    text = text.replace('"', "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def tts_bytes(text: str, voice: str, rate: str, retries: int = 4) -> bytes:
    import edge_tts

    cleaned = sanitize_for_tts(text)
    if not cleaned:
        return b""

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            communicate = edge_tts.Communicate(text=cleaned, voice=voice, rate=rate)
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            if chunks:
                await asyncio.sleep(0.15)
                return b"".join(chunks)
            raise RuntimeError("Empty audio received")
        except Exception as e:
            last_err = e
            if attempt < retries:
                await asyncio.sleep(0.8 * attempt)
            else:
                raise RuntimeError(f"TTS 실패 (text={cleaned[:80]!r}): {e}") from last_err
    return b""


async def convert_file(
    md_path: Path,
    out_dir: Path,
    voice: str,
    rate: str,
    mode: str,
    pause_sec: float,
    section_pause_sec: float,
    repeat: int,
    dry_run: bool,
) -> Path | None:
    content = md_path.read_text(encoding="utf-8")
    prefix = md_path.stem.split(".")[0]

    if "filler" in md_path.name.lower():
        sections = extract_filler_sections(content)
    else:
        sections = extract_topic_sections(content)

    if not sections:
        print(f"[SKIP] {md_path.name}: 추출된 영어 문장이 없습니다.")
        return None

    intro = TOPIC_TITLES.get(prefix, f"{md_path.stem}.")

    if dry_run:
        print(f"\n=== {md_path.name} ({intro}) ===")
        for idx, sec in enumerate(sections, 1):
            print(f"  [Part {idx}]")
            for line in sec:
                print(f"    {line}")
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{md_path.stem}.mp3"

    audio_parts: list[bytes] = []
    ref_mp3 = await tts_bytes(intro, voice=voice, rate=rate)
    audio_parts.append(ref_mp3)
    audio_parts.append(make_silence_mp3(1.2, ref_mp3))

    for sec_idx, sec_lines in enumerate(sections):
        if mode == "shadowing":
            sentences: list[str] = []
            for line in sec_lines:
                sentences.extend(split_sentences(line))
            for sent in sentences:
                sent_audio = await tts_bytes(sent, voice=voice, rate=rate)
                for _ in range(repeat):
                    audio_parts.append(sent_audio)
                    audio_parts.append(make_silence_mp3(pause_sec, ref_mp3))
        else:
            paragraph = " ".join(sec_lines)
            sec_audio = await tts_bytes(paragraph, voice=voice, rate=rate)
            for _ in range(repeat):
                audio_parts.append(sec_audio)
                if repeat > 1:
                    audio_parts.append(make_silence_mp3(pause_sec, ref_mp3))

        if sec_idx < len(sections) - 1:
            audio_parts.append(make_silence_mp3(section_pause_sec, ref_mp3))

    out_path.write_bytes(b"".join(audio_parts))
    size_kb = out_path.stat().st_size / 1024
    print(f"[OK] {out_path.name} ({size_kb:.1f} KB, {len(sections)}개 파트)", flush=True)
    return out_path


def generate_mobile_player(out_dir: Path, mp3_files: list[Path]) -> Path:
    """갤럭시 스마트폰 / 자동차 블루투스(Media Session API) 호환 웹 플레이어 생성"""
    import json

    tracks = []
    for p in sorted(mp3_files, key=lambda x: x.name):
        prefix = p.stem.split(".")[0]
        tracks.append(
            {
                "file": p.name,
                "title": p.stem,
                "subtitle": TOPIC_TITLES.get(prefix, p.stem).rstrip("."),
            }
        )

    tracks_json = json.dumps(tracks, ensure_ascii=False)
    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0" />
  <title>OPIc 출퇴근 영어 플레이어</title>
  <style>
    :root {{ --bg: #0f172a; --card: #1e293b; --accent: #38bdf8; --text: #f8fafc; --muted: #94a3b8; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 16px 16px 160px; }}
    h1 {{ font-size: 1.25rem; margin: 0 0 4px; }}
    .sub {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 16px; }}
    .track-list {{ display: flex; flex-direction: column; gap: 8px; }}
    .track {{ display: flex; align-items: center; justify-content: space-between; background: var(--card); padding: 14px 16px; border-radius: 12px; cursor: pointer; border: 1px solid transparent; transition: 0.15s; }}
    .track.active {{ border-color: var(--accent); background: #1e3a5f; }}
    .track-title {{ font-weight: 600; font-size: 1rem; }}
    .track-sub {{ font-size: 0.8rem; color: var(--muted); margin-top: 2px; }}
    .dl {{ color: var(--muted); text-decoration: none; font-size: 0.85rem; padding: 6px 10px; border-radius: 8px; background: rgba(255,255,255,0.06); }}
    .player-bar {{ position: fixed; bottom: 0; left: 0; right: 0; background: #111827; border-top: 1px solid #334155; padding: 12px 16px 18px; }}
    .now {{ font-size: 0.9rem; font-weight: 600; margin-bottom: 8px; color: var(--accent); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    audio {{ width: 100%; height: 40px; }}
    .controls {{ display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }}
    button {{ background: var(--card); color: var(--text); border: 1px solid #334155; border-radius: 8px; padding: 8px 12px; font-size: 0.85rem; cursor: pointer; flex: 1; }}
    button.on {{ border-color: var(--accent); color: var(--accent); }}
  </style>
</head>
<body>
  <h1>🎧 OPIc 출퇴근 플레이어</h1>
  <div class="sub">트랙을 누르면 순서대로 자동 재생됩니다 (화면 꺼짐 / 블루투스 지원)</div>
  <div class="track-list" id="list"></div>

  <div class="player-bar">
    <div class="now" id="nowPlaying">트랙을 선택하세요</div>
    <audio id="audio" controls playsinline></audio>
    <div class="controls">
      <button onclick="prevTrack()">⏮ 이전</button>
      <button onclick="nextTrack()">⏭ 다음</button>
      <button id="repeatBtn" onclick="toggleRepeat()">🔁 전체반복</button>
      <button id="speedBtn" onclick="cycleSpeed()">⚡ 1.0x</button>
    </div>
  </div>

  <script>
    const tracks = {tracks_json};
    const audio = document.getElementById("audio");
    const listEl = document.getElementById("list");
    const nowEl = document.getElementById("nowPlaying");
    const repeatBtn = document.getElementById("repeatBtn");
    const speedBtn = document.getElementById("speedBtn");

    let current = 0;
    let repeatMode = "all"; // "all" | "one"
    const speeds = [0.85, 0.9, 1.0, 1.1, 1.2];
    let speedIdx = 2;

    function render() {{
      listEl.innerHTML = "";
      tracks.forEach((t, idx) => {{
        const div = document.createElement("div");
        div.className = "track" + (idx === current ? " active" : "");
        div.innerHTML = `
          <div>
            <div class="track-title">${{t.title}}</div>
            <div class="track-sub">${{t.subtitle}}</div>
          </div>
          <a class="dl" href="${{encodeURIComponent(t.file)}}" download onclick="event.stopPropagation()">저장</a>
        `;
        div.onclick = () => playTrack(idx);
        listEl.appendChild(div);
      }});
    }}

    function playTrack(idx) {{
      if (!tracks.length) return;
      current = (idx + tracks.length) % tracks.length;
      const t = tracks[current];
      audio.src = encodeURIComponent(t.file);
      audio.playbackRate = speeds[speedIdx];
      audio.play().catch(() => {{}});
      nowEl.textContent = `▶ ${{t.title}} — ${{t.subtitle}}`;
      render();
      if ("mediaSession" in navigator) {{
        navigator.mediaSession.metadata = new MediaMetadata({{
          title: t.title,
          artist: "OPIc Scripts",
          album: t.subtitle,
        }});
        navigator.mediaSession.setActionHandler("previoustrack", prevTrack);
        navigator.mediaSession.setActionHandler("nexttrack", nextTrack);
      }}
    }}

    function nextTrack() {{ playTrack(current + 1); }}
    function prevTrack() {{ playTrack(current - 1); }}

    function toggleRepeat() {{
      repeatMode = repeatMode === "all" ? "one" : "all";
      repeatBtn.textContent = repeatMode === "one" ? "🔂 한곡반복" : "🔁 전체반복";
      repeatBtn.classList.toggle("on", repeatMode === "one");
    }}

    function cycleSpeed() {{
      speedIdx = (speedIdx + 1) % speeds.length;
      audio.playbackRate = speeds[speedIdx];
      speedBtn.textContent = `⚡ ${{speeds[speedIdx].toFixed(2).replace(/0$/, "")}}x`;
    }}

    audio.addEventListener("ended", () => {{
      if (repeatMode === "one") {{
        audio.currentTime = 0;
        audio.play();
      }} else {{
        nextTrack();
      }}
    }});

    render();
    if (tracks.length) {{
      audio.src = encodeURIComponent(tracks[0].file);
      nowEl.textContent = `${{tracks[0].title}} — ${{tracks[0].subtitle}}`;
    }}
  </script>
</body>
</html>
"""
    html_path = out_dir / "index.html"
    html_path.write_text(html, encoding="utf-8")
    return html_path


def collect_target_files(base_dir: Path, targets: list[str]) -> list[Path]:
    if targets:
        files = []
        for t in targets:
            p = Path(t)
            if not p.is_absolute():
                p = base_dir / p
            if p.exists():
                files.append(p)
            else:
                print(f"[WARN] 파일을 찾을 수 없습니다: {t}")
        return files

    files = sorted(base_dir.glob("*.md"))
    # 00.filler.md는 00.filler_readable.md와 중복되므로 제외
    result = [
        f
        for f in files
        if f.name.lower() not in {"00.filler.md", "readme.md"}
    ]
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="OPIc 마크다운 스크립트에서 영어만 추출해 출퇴근용 MP3로 변환합니다."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="변환할 특정 .md 파일 (비워두면 폴더 내 전체 스크립트 변환)",
    )
    parser.add_argument(
        "--mode",
        choices=["normal", "shadowing"],
        default="normal",
        help="normal: 파트별 자연스럽게 읽기 / shadowing: 문장마다 공백 넣고 따라 말하기용",
    )
    parser.add_argument(
        "--voice",
        default="en-US-GuyNeural",
        help="Edge TTS 음성 (추천: en-US-GuyNeural, en-US-AriaNeural, en-US-ChristopherNeural)",
    )
    parser.add_argument(
        "--rate",
        default="+0%",
        help="말하기 속도 (예: -10%% 느리게, +0%% 보통, +10%% 빠르게)",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=2.0,
        help="shadowing 모드 문장 사이 공백(초, 기본 2.0초)",
    )
    parser.add_argument(
        "--section-pause",
        type=float,
        default=2.0,
        help="파트(묘사/시제/롤플레이) 사이 공백(초, 기본 2.0초)",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="반복 횟수 (예: --repeat 2)",
    )
    parser.add_argument(
        "--src",
        default="",
        help="스크립트(.md) 폴더 경로 (기본: 저장소 루트의 scripts/)",
    )
    parser.add_argument(
        "--out",
        default="",
        help="MP3 저장 폴더 (기본: 저장소 루트의 mp3/ 또는 mp3_shadowing/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="MP3를 만들지 않고 추출된 영어 텍스트만 미리 출력",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    scripts_dir = Path(args.src) if args.src else repo_root / "scripts"
    if not scripts_dir.is_absolute():
        scripts_dir = repo_root / scripts_dir

    out_dir = Path(args.out) if args.out else repo_root / (
        "mp3_shadowing" if args.mode == "shadowing" else "mp3"
    )
    if not out_dir.is_absolute():
        out_dir = repo_root / out_dir

    files = collect_target_files(scripts_dir, args.files)
    if not files:
        print("변환할 .md 파일이 없습니다.")
        sys.exit(1)

    if not args.dry_run:
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            print("edge-tts가 설치되어 있지 않습니다. 먼저 설치해 주세요:")
            print("  pip install edge-tts")
            sys.exit(1)

    print(f"대상 파일 {len(files)}개 | 모드: {args.mode} | 음성: {args.voice} | 속도: {args.rate}", flush=True)
    generated: list[Path] = []
    for md_file in files:
        res = await convert_file(
            md_path=md_file,
            out_dir=out_dir,
            voice=args.voice,
            rate=args.rate,
            mode=args.mode,
            pause_sec=args.pause,
            section_pause_sec=args.section_pause,
            repeat=args.repeat,
            dry_run=args.dry_run,
        )
        if res is not None:
            generated.append(res)

    if not args.dry_run:
        all_mp3s = sorted(out_dir.glob("*.mp3"))
        if all_mp3s:
            player_path = generate_mobile_player(out_dir, all_mp3s)
            print(f"[OK] 모바일 플레이어 생성: {player_path.name}", flush=True)
        print(f"\n완료! 저장 폴더: {out_dir}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
