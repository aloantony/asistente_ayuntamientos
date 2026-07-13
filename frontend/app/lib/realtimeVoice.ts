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
  ) {
    const peerConnection = new RTCPeerConnection();
    const audioElement = document.createElement("audio");
    audioElement.autoplay = true;
    audioElement.setAttribute("playsinline", "true");
    audioElement.style.display = "none";
    document.body.appendChild(audioElement);
    peerConnection.ontrack = (event) => {
      audioElement.srcObject = event.streams[0];
    };

    const mediaStream = await navigator.mediaDevices.getUserMedia(
      REALTIME_AUDIO_CONSTRAINTS,
    );
    const [audioTrack] = mediaStream.getAudioTracks();
    if (!audioTrack) {
      mediaStream.getTracks().forEach((track) => track.stop());
      peerConnection.close();
      throw new ApiRequestError("No se pudo acceder al micrófono.", 0);
    }
    peerConnection.addTrack(audioTrack, mediaStream);

    const dataChannel = peerConnection.createDataChannel("oai-events");
    const voiceSession = new RealtimeVoiceSession(
      peerConnection,
      dataChannel,
      mediaStream,
      audioElement,
      handlers,
    );
    dataChannel.addEventListener("open", () => handlers.onOpen?.());
    dataChannel.addEventListener("close", () => handlers.onClose?.());
    dataChannel.addEventListener("error", (event) =>
      handlers.onError?.(event),
    );
    dataChannel.addEventListener("message", (event) => {
      try {
        handlers.onEvent?.(JSON.parse(event.data) as RealtimeServerEvent);
      } catch (parseError) {
        handlers.onError?.(parseError);
      }
    });

    try {
      const offer = await peerConnection.createOffer();
      await peerConnection.setLocalDescription(offer);
      const sdpResponse = await fetch(session.realtime_url, {
        method: "POST",
        body: offer.sdp,
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
    } catch (error) {
      voiceSession.stop();
      throw error;
    }

    return voiceSession;
  }

  stop() {
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
    if (this.dataChannel.readyState !== "open") {
      throw new ApiRequestError("La sesión de voz no está conectada.", 0);
    }
    this.dataChannel.send(JSON.stringify(event));
  }

  sendFunctionOutput(callId: string, output: string) {
    this.sendEvent({
      type: "conversation.item.create",
      item: {
        type: "function_call_output",
        call_id: callId,
        output,
      },
    });
    this.sendEvent({ type: "response.create" });
  }
}
