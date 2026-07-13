import type { AssistantRealtimeSession } from "../components/types";
import { ApiRequestError } from "./api";

const REALTIME_AUDIO_CONSTRAINTS: MediaStreamConstraints = {
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
};

export type RealtimeServerEvent = {
  type?: string;
  [key: string]: unknown;
};

type RealtimeVoiceSessionHandlers = {
  onOpen?: () => void;
  onClose?: () => void;
  onEvent?: (event: RealtimeServerEvent) => void;
  onError?: (error: unknown) => void;
};

export class RealtimeVoiceSession {
  private stopped = false;
  private closeNotified = false;
  private closeNotificationEnabled = true;
  private abortSignal: AbortSignal | null = null;
  private abortHandler: (() => void) | null = null;

  private constructor(
    private readonly peerConnection: RTCPeerConnection,
    private readonly dataChannel: RTCDataChannel,
    private readonly mediaStream: MediaStream,
    private readonly audioElement: HTMLAudioElement,
    private readonly handlers: RealtimeVoiceSessionHandlers,
  ) {}

  static async start(
    session: AssistantRealtimeSession,
    handlers: RealtimeVoiceSessionHandlers,
    signal?: AbortSignal,
  ) {
    const peerConnection = new RTCPeerConnection();
    let mediaStream: MediaStream | null = null;
    let audioElement: HTMLAudioElement | null = null;
    let voiceSession: RealtimeVoiceSession | null = null;

    try {
      signal?.throwIfAborted();
      mediaStream = await navigator.mediaDevices.getUserMedia(
        REALTIME_AUDIO_CONSTRAINTS,
      );
      signal?.throwIfAborted();
      const [audioTrack] = mediaStream.getAudioTracks();
      if (!audioTrack) {
        throw new ApiRequestError("No se pudo acceder al micrófono.", 0);
      }

      audioElement = document.createElement("audio");
      audioElement.autoplay = true;
      audioElement.setAttribute("playsinline", "true");
      audioElement.style.display = "none";
      document.body.appendChild(audioElement);
      peerConnection.ontrack = (event) => {
        if (audioElement) {
          audioElement.srcObject = event.streams[0];
        }
      };
      peerConnection.addTrack(audioTrack, mediaStream);

      const dataChannel = peerConnection.createDataChannel("oai-events");
      voiceSession = new RealtimeVoiceSession(
        peerConnection,
        dataChannel,
        mediaStream,
        audioElement,
        handlers,
      );
      voiceSession.bindAbortSignal(signal);
      dataChannel.addEventListener("open", () => {
        if (!voiceSession?.isClosed) {
          handlers.onOpen?.();
        }
      });
      dataChannel.addEventListener("close", () =>
        voiceSession?.handleTransportClose(),
      );
      dataChannel.addEventListener("error", (event) => {
        handlers.onError?.(event);
        voiceSession?.handleTransportClose();
      });
      dataChannel.addEventListener("message", (event) => {
        if (voiceSession?.isClosed) {
          return;
        }
        try {
          handlers.onEvent?.(JSON.parse(event.data) as RealtimeServerEvent);
        } catch (parseError) {
          handlers.onError?.(parseError);
        }
      });
      peerConnection.addEventListener("connectionstatechange", () => {
        if (
          peerConnection.connectionState === "failed" ||
          peerConnection.connectionState === "closed"
        ) {
          voiceSession?.handleTransportClose();
        }
      });

      const offer = await peerConnection.createOffer();
      await peerConnection.setLocalDescription(offer);
      const sdpResponse = await fetch(session.realtime_url, {
        method: "POST",
        body: offer.sdp,
        signal,
        headers: {
          Authorization: `Bearer ${session.client_secret}`,
          "Content-Type": "application/sdp",
        },
      });
      if (!sdpResponse.ok) {
        throw new ApiRequestError(
          "No se pudo conectar la voz en tiempo real.",
          sdpResponse.status,
        );
      }
      await peerConnection.setRemoteDescription({
        type: "answer",
        sdp: await sdpResponse.text(),
      });
      signal?.throwIfAborted();
      if (voiceSession.isClosed) {
        throw new ApiRequestError("La sesión de voz se cerró al conectar.", 0);
      }
    } catch (error) {
      if (voiceSession) {
        voiceSession.stop();
      } else {
        mediaStream?.getTracks().forEach((track) => track.stop());
        peerConnection.close();
        audioElement?.pause();
        if (audioElement) {
          audioElement.srcObject = null;
          audioElement.remove();
        }
      }
      throw error;
    }

    return voiceSession;
  }

  get isClosed() {
    return this.stopped;
  }

  stop() {
    this.closeNotificationEnabled = false;
    this.dispose();
  }

  private bindAbortSignal(signal?: AbortSignal) {
    if (!signal) {
      return;
    }
    this.abortSignal = signal;
    this.abortHandler = () => this.stop();
    signal.addEventListener("abort", this.abortHandler, { once: true });
  }

  private handleTransportClose() {
    const shouldNotify = this.closeNotificationEnabled;
    this.dispose();
    if (shouldNotify && !this.closeNotified) {
      this.closeNotified = true;
      this.handlers.onClose?.();
    }
  }

  private dispose() {
    if (this.stopped) {
      return;
    }
    this.stopped = true;
    if (this.abortSignal && this.abortHandler) {
      this.abortSignal.removeEventListener("abort", this.abortHandler);
    }
    this.abortSignal = null;
    this.abortHandler = null;
    this.mediaStream.getTracks().forEach((track) => track.stop());
    if (this.dataChannel.readyState !== "closed") {
      this.dataChannel.close();
    }
    this.peerConnection.close();
    this.audioElement.pause();
    this.audioElement.srcObject = null;
    this.audioElement.remove();
  }

  sendEvent(event: Record<string, unknown>) {
    if (this.stopped || this.dataChannel.readyState !== "open") {
      throw new ApiRequestError("La sesión de voz no está conectada.", 0);
    }
    this.dataChannel.send(JSON.stringify(event));
  }

  setInputEnabled(enabled: boolean) {
    if (this.stopped) {
      return;
    }
    this.mediaStream
      .getAudioTracks()
      .forEach((track) => (track.enabled = enabled));
  }

  sendFunctionOutputItem(callId: string, output: string) {
    this.sendEvent({
      type: "conversation.item.create",
      item: {
        type: "function_call_output",
        call_id: callId,
        output,
      },
    });
  }

  requestResponse(
    options: {
      eventId?: string;
      instructions?: string;
      metadata?: Record<string, string>;
      toolChoice?: "auto" | "none";
    } = {},
  ) {
    const response: Record<string, unknown> = {};
    if (options.instructions) {
      response.instructions = options.instructions;
    }
    if (options.metadata) {
      response.metadata = options.metadata;
    }
    if (options.toolChoice) {
      response.tool_choice = options.toolChoice;
    }
    this.sendEvent({
      type: "response.create",
      ...(options.eventId ? { event_id: options.eventId } : {}),
      ...(Object.keys(response).length > 0 ? { response } : {}),
    });
  }
}
