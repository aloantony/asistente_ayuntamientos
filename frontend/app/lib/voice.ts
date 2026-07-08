const DETAIL_PLACEHOLDER = "Te dejo el detalle escrito en pantalla.";

type SpeechPlayerListener = (speaking: boolean) => void;

type SpeechQueueItem = {
  text: string;
  resolve: () => void;
  reject: (error: unknown) => void;
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
