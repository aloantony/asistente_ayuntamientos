const DETAIL_PLACEHOLDER = "Te dejo el detalle escrito en pantalla.";
const SENTENCE_MIN_CHARS = 60;
const SILENCE_CHECK_INTERVAL_MS = 100;
const SILENCE_CALIBRATION_MS = 500;
const SILENCE_AFTER_VOICE_MS = 1400;
const NO_VOICE_TIMEOUT_MS = 15000;
const MAX_UTTERANCE_MS = 60000;
const MIN_RMS_THRESHOLD = 0.01;
const NOISE_MULTIPLIER = 3;

type SpeechPlayerListener = (speaking: boolean) => void;

type SpeechQueueItem = {
  text: string;
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
      const abortController = new AbortController();
      currentAbortController = abortController;

      try {
        const audio = await deps.synthesize(item.text, abortController.signal);
        if (!abortController.signal.aborted) {
          await playBlob(audio, abortController.signal);
        }
        item.resolve();
      } catch (error) {
        if (abortController.signal.aborted) {
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
        queue.push({ text: normalized, resolve, reject });
        void processQueue();
      });
    },
    stop(): void {
      currentAbortController?.abort();
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

  const samples = new Uint8Array(analyser.fftSize);
  const startedAt = performance.now();
  let calibrationTotal = 0;
  let calibrationSamples = 0;
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

    if (elapsed <= SILENCE_CALIBRATION_MS) {
      calibrationTotal += rms;
      calibrationSamples += 1;
      return;
    }

    if (calibrationSamples > 0) {
      threshold = Math.max(
        MIN_RMS_THRESHOLD,
        (calibrationTotal / calibrationSamples) * NOISE_MULTIPLIER,
      );
      calibrationSamples = 0;
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
