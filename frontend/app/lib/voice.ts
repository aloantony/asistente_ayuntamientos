const DETAIL_PLACEHOLDER = "Te dejo el detalle escrito en pantalla.";
const SENTENCE_MIN_CHARS = 60;
const SILENCE_CHECK_INTERVAL_MS = 100;
const SILENCE_CALIBRATION_MS = 500;
const SILENCE_AFTER_VOICE_MS = 1400;
const NO_VOICE_TIMEOUT_MS = 15000;
const MAX_UTTERANCE_MS = 60000;
const MIN_RMS_THRESHOLD = 0.01;
const MAX_RMS_THRESHOLD = 0.045;
const NOISE_MULTIPLIER = 3;
// Barge-in (interrupting the assistant by speaking over it). Tuned
// conservatively so residual echo of the assistant's own voice and street noise
// do not cut it off: the user's speech must exceed the ambient/echo floor by a
// wide margin and stay above it. There is no upper cap on the threshold — in
// loud places it rises with the noise so only clearly louder speech barges in.
const BARGE_IN_SETTLE_MS = 350;
const BARGE_IN_SUSTAIN_MS = 300;
const BARGE_IN_MIN_THRESHOLD = 0.03;
const BARGE_IN_MULTIPLIER = 4;

type SpeechPlayerListener = (speaking: boolean) => void;

type SpeechQueueItem = {
  text: string;
  // Synthesis is started as soon as the sentence is queued (not when playback
  // reaches it), so later sentences are generated while the current one plays.
  audio: Promise<Blob>;
  abortController: AbortController;
  resolve: () => void;
  reject: (error: unknown) => void;
};

type SilenceDetectorOptions = {
  onSilence: () => void;
  onTimeout: () => void;
};

function ensureSentenceEnding(text: string) {
  const trimmed = text.trim();
  if (!trimmed) {
    return "";
  }
  return /[.!?…]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

export function flattenMarkdownForSpeech(markdown: string): string {
  let detailInserted = false;
  const replaceDetail = () => {
    if (detailInserted) {
      return "\n";
    }
    detailInserted = true;
    return `\n${DETAIL_PLACEHOLDER}\n`;
  };

  const withoutBlocks = markdown
    .replace(/```[\s\S]*?```/g, replaceDetail)
    .replace(/^\s*\|.*\|\s*$/gm, replaceDetail);
  const withoutLinks = withoutBlocks.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  const withoutMarkers = withoutLinks.replace(/[#*_>`]/g, "");
  const listNormalized = withoutMarkers
    .split("\n")
    .map((line) => {
      const unordered = line.match(/^\s*-\s+(.+)$/);
      if (unordered) {
        return ensureSentenceEnding(unordered[1]);
      }
      const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
      if (ordered) {
        return ensureSentenceEnding(ordered[1]);
      }
      return line;
    })
    .join("\n");

  return listNormalized
    .replace(/[ \t]+/g, " ")
    .replace(/\s*\n+\s*/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();
}

export function createSpeechPlayer(deps: {
  synthesize(text: string, signal?: AbortSignal): Promise<Blob>;
}) {
  const queue: SpeechQueueItem[] = [];
  const listeners = new Set<SpeechPlayerListener>();
  let speaking = false;
  let processing = false;
  let currentItem: SpeechQueueItem | null = null;
  let currentAudio: HTMLAudioElement | null = null;
  let currentObjectUrl: string | null = null;
  let currentAbortController: AbortController | null = null;

  function notify(nextSpeaking: boolean) {
    if (speaking === nextSpeaking) {
      return;
    }
    speaking = nextSpeaking;
    listeners.forEach((listener) => listener(speaking));
  }

  function releaseCurrentAudio() {
    if (currentAudio) {
      currentAudio.pause();
      currentAudio.removeAttribute("src");
      currentAudio.load();
      currentAudio = null;
    }
    if (currentObjectUrl) {
      URL.revokeObjectURL(currentObjectUrl);
      currentObjectUrl = null;
    }
  }

  function playBlob(blob: Blob, signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      const audio = new Audio();
      const objectUrl = URL.createObjectURL(blob);
      let settled = false;
      currentAudio = audio;
      currentObjectUrl = objectUrl;

      const finish = (callback: () => void) => {
        if (settled) {
          return;
        }
        settled = true;
        audio.onended = null;
        audio.onerror = null;
        signal.removeEventListener("abort", handleAbort);
        callback();
      };
      const handleAbort = () => {
        audio.pause();
        finish(resolve);
      };

      audio.onended = () => finish(resolve);
      audio.onerror = () =>
        finish(() => reject(new Error("No se pudo reproducir la voz.")));
      signal.addEventListener("abort", handleAbort, { once: true });
      audio.src = objectUrl;
      audio.play().catch((error: unknown) => finish(() => reject(error)));
    });
  }

  async function processQueue() {
    if (processing) {
      return;
    }
    processing = true;
    notify(queue.length > 0);

    while (queue.length > 0) {
      notify(true);
      const item = queue.shift();
      if (!item) {
        continue;
      }
      currentItem = item;
      currentAbortController = item.abortController;

      try {
        const audio = await item.audio;
        if (!item.abortController.signal.aborted) {
          await playBlob(audio, item.abortController.signal);
        }
        item.resolve();
      } catch (error) {
        if (item.abortController.signal.aborted) {
          item.resolve();
        } else {
          item.reject(error);
        }
      } finally {
        releaseCurrentAudio();
        currentAbortController = null;
        currentItem = null;
      }
    }

    processing = false;
    notify(false);
  }

  return {
    speak(text: string): Promise<void> {
      const normalized = text.trim();
      if (!normalized) {
        return Promise.resolve();
      }
      return new Promise((resolve, reject) => {
        // Start synthesis immediately so it overlaps playback of earlier
        // sentences and there is no gap at each sentence boundary.
        const abortController = new AbortController();
        const audio = deps.synthesize(normalized, abortController.signal);
        // Prevent an unhandled rejection if the item is stopped/aborted before
        // the queue consumes it; the queue still awaits `audio` for real errors.
        audio.catch(() => undefined);
        queue.push({ text: normalized, audio, abortController, resolve, reject });
        void processQueue();
      });
    },
    stop(): void {
      currentAbortController?.abort();
      queue.forEach((item) => item.abortController.abort());
      releaseCurrentAudio();
      while (queue.length > 0) {
        queue.shift()?.resolve();
      }
      currentItem?.resolve();
      currentItem = null;
      notify(false);
    },
    subscribe(listener: SpeechPlayerListener) {
      listeners.add(listener);
      listener(speaking);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

export function createSentenceChunker(onSentence: (sentence: string) => void) {
  let buffer = "";

  function emitReadySentences(force = false) {
    while (buffer.trim().length > 0) {
      const boundary = findSentenceBoundary(buffer);
      if (boundary === -1) {
        break;
      }
      const sentence = buffer.slice(0, boundary).trim();
      if (!force && sentence.length < SENTENCE_MIN_CHARS) {
        break;
      }
      buffer = buffer.slice(boundary).trimStart();
      if (sentence) {
        onSentence(sentence);
      }
    }

    if (force) {
      const remaining = buffer.trim();
      buffer = "";
      if (remaining) {
        onSentence(remaining);
      }
    }
  }

  return {
    push(delta: string) {
      buffer += delta;
      emitReadySentences(false);
    },
    flush() {
      emitReadySentences(true);
    },
  };
}

export function splitTextForSpeech(text: string, maxChars: number) {
  const limit = Math.max(1, Math.floor(maxChars));
  let remaining = Array.from(text.trim());
  const chunks: string[] = [];

  while (remaining.length > limit) {
    let boundary = -1;
    for (let index = limit - 1; index >= 0; index -= 1) {
      const char = remaining[index];
      const next = remaining[index + 1] ?? "";
      if (char === "\n" || (".!?…".includes(char) && /\s/.test(next))) {
        boundary = index + 1;
        break;
      }
    }
    if (boundary < 1) {
      for (let index = limit - 1; index >= 0; index -= 1) {
        if (/\s/.test(remaining[index])) {
          boundary = index + 1;
          break;
        }
      }
    }
    if (boundary < 1) {
      boundary = limit;
    }

    const chunk = remaining.slice(0, boundary).join("").trim();
    remaining = Array.from(remaining.slice(boundary).join("").trimStart());
    if (chunk) {
      chunks.push(chunk);
    }
  }

  const finalChunk = remaining.join("").trim();
  if (finalChunk) {
    chunks.push(finalChunk);
  }
  return chunks;
}

function findSentenceBoundary(text: string) {
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1] ?? "";
    if (char === "\n") {
      return index + 1;
    }
    if (".!?…".includes(char) && /\s/.test(next)) {
      return index + 1;
    }
  }
  return -1;
}

export function createSilenceDetector(
  stream: MediaStream,
  opts: SilenceDetectorOptions,
) {
  const AudioContextConstructor =
    window.AudioContext ??
    (window as typeof window & { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!AudioContextConstructor) {
    const timeout = window.setTimeout(opts.onTimeout, NO_VOICE_TIMEOUT_MS);
    return {
      stop() {
        window.clearTimeout(timeout);
      },
    };
  }

  const audioContext = new AudioContextConstructor();
  const source = audioContext.createMediaStreamSource(stream);
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 2048;
  source.connect(analyser);
  // A context created outside a direct user gesture (e.g. the mic auto-armed on
  // entering voice mode) can start suspended; the analyser then reads silence
  // and the loop never detects the end of speech. Resume it defensively.
  void audioContext.resume().catch(() => undefined);

  const samples = new Uint8Array(analyser.fftSize);
  const startedAt = performance.now();
  let noiseFloor: number | null = null;
  let threshold = MIN_RMS_THRESHOLD;
  let heardVoice = false;
  let lastVoiceAt = 0;
  let finished = false;
  let stopped = false;

  function finish(callback: () => void) {
    if (finished) {
      return;
    }
    finished = true;
    stop();
    callback();
  }

  function currentRms() {
    analyser.getByteTimeDomainData(samples);
    let sum = 0;
    for (const sample of samples) {
      const normalized = (sample - 128) / 128;
      sum += normalized * normalized;
    }
    return Math.sqrt(sum / samples.length);
  }

  const interval = window.setInterval(() => {
    const now = performance.now();
    const elapsed = now - startedAt;
    const rms = currentRms();

    // Track the background noise floor as the quietest level seen (with a slow
    // upward drift) rather than averaging the first samples: a user who starts
    // talking the instant the mic opens would otherwise bake their own voice
    // into the floor, pushing the threshold above their speech so the loop
    // never registers voice — and never auto-stops on silence. The threshold is
    // also capped so a noisy calibration can't hide normal speech.
    if (noiseFloor === null || rms < noiseFloor) {
      noiseFloor = rms;
    } else {
      noiseFloor = noiseFloor * 0.98 + rms * 0.02;
    }
    threshold = Math.min(
      MAX_RMS_THRESHOLD,
      Math.max(MIN_RMS_THRESHOLD, noiseFloor * NOISE_MULTIPLIER),
    );

    // Brief settle window before acting on levels (mic gain / context resume).
    if (elapsed <= SILENCE_CALIBRATION_MS) {
      return;
    }

    if (rms >= threshold) {
      heardVoice = true;
      lastVoiceAt = now;
      return;
    }

    if (!heardVoice && elapsed >= NO_VOICE_TIMEOUT_MS) {
      finish(opts.onTimeout);
      return;
    }

    if (heardVoice && now - lastVoiceAt >= SILENCE_AFTER_VOICE_MS) {
      finish(opts.onSilence);
      return;
    }

    if (heardVoice && elapsed >= MAX_UTTERANCE_MS) {
      finish(opts.onSilence);
    }
  }, SILENCE_CHECK_INTERVAL_MS);

  function stop() {
    if (stopped) {
      return;
    }
    stopped = true;
    window.clearInterval(interval);
    source.disconnect();
    analyser.disconnect();
    void audioContext.close().catch(() => undefined);
  }

  return { stop };
}

type BargeInDetectorOptions = {
  onSpeech: () => void;
};

// Watches the microphone while the assistant is speaking and fires `onSpeech`
// when the user talks over it, so the caller can cut the playback and start
// listening (full-duplex barge-in). Unlike the silence detector it deliberately
// leaves the MediaStream tracks running: the caller owns the stream and may
// hand it straight to the listening phase.
export function createBargeInDetector(
  stream: MediaStream,
  opts: BargeInDetectorOptions,
) {
  const AudioContextConstructor =
    window.AudioContext ??
    (window as typeof window & { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!AudioContextConstructor) {
    return {
      stop() {},
    };
  }

  const audioContext = new AudioContextConstructor();
  const source = audioContext.createMediaStreamSource(stream);
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 2048;
  source.connect(analyser);
  void audioContext.resume().catch(() => undefined);

  const samples = new Uint8Array(analyser.fftSize);
  const startedAt = performance.now();
  let noiseFloor: number | null = null;
  let voiceSince: number | null = null;
  let finished = false;
  let stopped = false;

  function currentRms() {
    analyser.getByteTimeDomainData(samples);
    let sum = 0;
    for (const sample of samples) {
      const normalized = (sample - 128) / 128;
      sum += normalized * normalized;
    }
    return Math.sqrt(sum / samples.length);
  }

  const interval = window.setInterval(() => {
    const now = performance.now();
    const elapsed = now - startedAt;
    const rms = currentRms();

    if (noiseFloor === null || rms < noiseFloor) {
      noiseFloor = rms;
    } else {
      noiseFloor = noiseFloor * 0.98 + rms * 0.02;
    }
    const threshold = Math.max(
      BARGE_IN_MIN_THRESHOLD,
      noiseFloor * BARGE_IN_MULTIPLIER,
    );

    // Ignore the first moments so the ambient/echo floor can settle and the
    // playback onset transient is not mistaken for the user speaking.
    if (elapsed <= BARGE_IN_SETTLE_MS) {
      return;
    }

    if (rms >= threshold) {
      if (voiceSince === null) {
        voiceSince = now;
      } else if (!finished && now - voiceSince >= BARGE_IN_SUSTAIN_MS) {
        finished = true;
        stop();
        opts.onSpeech();
      }
    } else {
      voiceSince = null;
    }
  }, SILENCE_CHECK_INTERVAL_MS);

  function stop() {
    if (stopped) {
      return;
    }
    stopped = true;
    window.clearInterval(interval);
    source.disconnect();
    analyser.disconnect();
    void audioContext.close().catch(() => undefined);
  }

  return { stop };
}
