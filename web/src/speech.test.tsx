import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MicButton } from "./components/MicButton";
import { resetSpeechSource, speak, stopSpeaking } from "./speech";

interface Call {
  url: string;
  method: string;
  contentType: string | null;
  body: unknown;
}

/** A server that says whether ElevenLabs is configured and answers the two speech routes. */
function serve(options: { elevenlabs: boolean; sayStatus?: number; heard?: string }) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const headers = new Headers(init?.headers);
      calls.push({
        url,
        method: init?.method ?? "GET",
        contentType: headers.get("Content-Type"),
        body: init?.body,
      });
      if (url === "/api/speech") {
        return new Response(JSON.stringify({ elevenlabs: options.elevenlabs }));
      }
      if (url === "/api/speech/say") {
        const status = options.sayStatus ?? 200;
        return new Response(status === 200 ? "mp3-bytes" : "{}", { status });
      }
      if (url === "/api/speech/hear") {
        return new Response(JSON.stringify({ text: options.heard ?? "" }));
      }
      throw new Error(`unexpected request ${url}`);
    }),
  );
  return calls;
}

class FakeAudio {
  static played: string[] = [];
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public src: string) {}
  play() {
    FakeAudio.played.push(this.src);
    return Promise.resolve();
  }
  pause() {}
}

class FakeRecorder {
  static last: FakeRecorder | null = null;
  state = "inactive";
  mimeType = "audio/webm;codecs=opus";
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  constructor(public stream: unknown) {
    FakeRecorder.last = this;
  }
  start() {
    this.state = "recording";
  }
  stop() {
    this.state = "inactive";
    this.ondataavailable?.({ data: new Blob(["recorded audio"]) });
    this.onstop?.();
  }
}

const browserSpeak = vi.fn();

beforeEach(() => {
  resetSpeechSource();
  FakeAudio.played = [];
  FakeRecorder.last = null;
  browserSpeak.mockClear();
  vi.stubGlobal("Audio", FakeAudio);
  vi.stubGlobal("speechSynthesis", { speak: browserSpeak, cancel: vi.fn() });
  vi.stubGlobal(
    "SpeechSynthesisUtterance",
    class {
      rate = 1;
      constructor(public text: string) {}
    },
  );
  URL.createObjectURL = vi.fn(() => "blob:operator-line");
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  stopSpeaking();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, "mediaDevices");
});

describe("speak", () => {
  it("plays the operator's line with ElevenLabs when the server has a key", async () => {
    const calls = serve({ elevenlabs: true });
    speak("Sending 250 dollars to Sam.");
    await waitFor(() => expect(FakeAudio.played).toEqual(["blob:operator-line"]));
    const said = calls.find((call) => call.url === "/api/speech/say");
    expect(said?.method).toBe("POST");
    expect(JSON.parse(String(said?.body))).toEqual({ text: "Sending 250 dollars to Sam." });
    expect(browserSpeak).not.toHaveBeenCalled();
  });

  it("falls back to the browser's voice when ElevenLabs fails", async () => {
    serve({ elevenlabs: true, sayStatus: 503 });
    speak("Sending 250 dollars to Sam.");
    await waitFor(() => expect(browserSpeak).toHaveBeenCalledTimes(1));
    expect(FakeAudio.played).toEqual([]);
  });

  it("uses the browser's voice when the server has no key", async () => {
    const calls = serve({ elevenlabs: false });
    speak("Sending 250 dollars to Sam.");
    await waitFor(() => expect(browserSpeak).toHaveBeenCalledTimes(1));
    expect(calls.some((call) => call.url === "/api/speech/say")).toBe(false);
  });

  it("does not play a line that was replaced before its audio arrived", async () => {
    serve({ elevenlabs: true });
    speak("first line");
    speak("second line");
    await waitFor(() => expect(FakeAudio.played).toHaveLength(1));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(FakeAudio.played).toHaveLength(1);
  });
});

describe("MicButton with ElevenLabs", () => {
  it("records, sends the recording for transcription, and passes the sentence on", async () => {
    const calls = serve({ elevenlabs: true, heard: "Send 20 dollars to Sam." });
    const stopTrack = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] })) },
    });
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    const onText = vi.fn();
    const onFinal = vi.fn();
    const user = userEvent.setup();
    render(<MicButton onText={onText} onFinal={onFinal} />);

    const button = await screen.findByRole("button", { name: "Speak" });
    await waitFor(() => expect(button).toHaveAttribute("title", "Speech to text by ElevenLabs"));
    await user.click(button);
    await waitFor(() => expect(FakeRecorder.last?.state).toBe("recording"));
    expect(screen.getByRole("button", { name: "Listening…" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Listening…" }));
    await waitFor(() => expect(onFinal).toHaveBeenCalledWith("Send 20 dollars to Sam."));
    expect(onFinal).toHaveBeenCalledTimes(1);
    const heard = calls.find((call) => call.url === "/api/speech/hear");
    expect(heard?.method).toBe("POST");
    expect(heard?.contentType).toBe("audio/webm;codecs=opus");
    expect(heard?.body).toBeInstanceOf(Blob);
    expect(stopTrack).toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "Speak" })).toBeInTheDocument();
  });

  it("says so when the microphone is refused", async () => {
    serve({ elevenlabs: true });
    const refused = Object.assign(new Error("denied"), { name: "NotAllowedError" });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => Promise.reject(refused)) },
    });
    vi.stubGlobal("MediaRecorder", FakeRecorder);
    const user = userEvent.setup();
    render(<MicButton onText={() => undefined} onFinal={() => undefined} />);
    const button = await screen.findByRole("button", { name: "Speak" });
    await user.click(button);
    expect(await screen.findByText("Microphone access was refused.")).toBeInTheDocument();
  });
});
