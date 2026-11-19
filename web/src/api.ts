import type {
  ApprovalView,
  BotNote,
  ConnectorCreate,
  ConnectorCreated,
  ConnectorList,
  ConnectorUpdate,
  ConnectorView,
  DailySummary,
  ExecuteResponse,
  ForecastList,
  HostileResult,
  IntentResponse,
  MarketBrief,
  MarketListing,
  Opinion,
  OkResponse,
  RuleProposal,
  RuleView,
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
  deskMarkets: async () =>
    (await request<{ markets: MarketListing[] }>("/api/desk/markets")).markets,
  deskBrief: (slug: string, qty: number) =>
    request<MarketBrief>(`/api/desk/brief?slug=${encodeURIComponent(slug)}&qty=${qty}`),
  deskMove: (slug: string, qty: number, mode: "post" | "take") =>
    post<{ move_id: string }>("/api/desk/move", { slug, qty, mode }),
  forecast: (slug: string, qty: number) => post<Opinion>("/api/forecasts", { slug, qty }),
  forecasts: () => request<ForecastList>("/api/forecasts"),
  overrideForecast: (receiptId: string) =>
    post<Opinion>(`/api/forecasts/${encodeURIComponent(receiptId)}/override`, {}),
  rules: async () => (await request<{ rules: RuleView[] }>("/api/rules")).rules,
  proposeRule: (text: string) => post<RuleProposal>("/api/rules/propose", { text }),
  confirmRule: (proposalId: string, challenge: string) =>
    post<OkResponse>(`/api/rules/${encodeURIComponent(proposalId)}/confirm`, { challenge }),
  cancelRule: (proposalId: string) =>
    post<OkResponse>(`/api/rules/${encodeURIComponent(proposalId)}/cancel`, {}),
  summary: () => request<DailySummary>("/api/summary"),
  researchNotes: async (slug: string) =>
    (
      await request<{ notes: BotNote[] }>(
        `/api/notes?routine=equivalence&subject=${encodeURIComponent(slug)}`,
      )
    ).notes,
  hostile: (content: string) => post<HostileResult>("/api/hostile", { content }),
  connectors: () => request<ConnectorList>("/api/connectors"),
  createConnector: (body: ConnectorCreate) => post<ConnectorCreated>("/api/connectors", body),
  updateConnector: (id: number, body: ConnectorUpdate) =>
    request<ConnectorView>(`/api/connectors/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  revokeConnector: (id: number) =>
    request<OkResponse>(`/api/connectors/${id}`, { method: "DELETE" }),
  revealForecast: (receiptId: string, outcome: boolean) =>
    post<Opinion>(`/api/forecasts/${encodeURIComponent(receiptId)}/reveal`, { outcome }),
};
