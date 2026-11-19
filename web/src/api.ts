import type {
  ApprovalView,
  ExecuteResponse,
  IntentResponse,
  OkResponse,
  StatsResponse,
  SwitchboardEvent,
  Venue,
  Wallet,
} from "./types";

/** An error whose message is safe to show: the server's own `detail` when it sent one. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError("Cannot reach the Switchboard server. Is it running on port 8787?", 0);
  }
  if (!response.ok) {
    let detail = `The server answered ${response.status}.`;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // keep the generic message
    }
    throw new ApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) });
}

export const api = {
  intent: (text: string) => post<IntentResponse>("/api/intent", { text }),
  execute: (planId: string, routeId: "cheapest" | "fastest") =>
    post<ExecuteResponse>(`/api/plans/${encodeURIComponent(planId)}/execute`, {
      route_id: routeId,
    }),
  stats: () => request<StatsResponse>("/api/stats"),
  approvals: async () =>
    (await request<{ approvals: ApprovalView[] }>("/api/approvals")).approvals,
  simulateVoice: (requestId: string, amountSpoken: string, challenge: string) =>
    post<OkResponse>(`/api/approvals/${encodeURIComponent(requestId)}/simulate-voice`, {
      amount_spoken: amountSpoken,
      challenge,
    }),
  denyApproval: (requestId: string) =>
    post<OkResponse>(`/api/approvals/${encodeURIComponent(requestId)}/deny`, {}),
  freeze: (frozen: boolean) => post<OkResponse>("/api/freeze", { frozen }),
  setStreamStale: (stale: boolean) => post<OkResponse>("/api/chain/stub/stream", { stale }),
  history: async (afterId = 0) =>
    (
      await request<{ events: SwitchboardEvent[] }>(
        `/api/events/history?after_id=${afterId}&limit=5000`,
      )
    ).events,
  wallets: async () => (await request<{ wallets: Wallet[] }>("/api/wallets")).wallets,
  venues: async () => (await request<{ venues: Venue[] }>("/api/venues")).venues,
};
