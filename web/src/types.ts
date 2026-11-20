// Short names for the generated API types. Nothing here is written by hand except aliases:
// the shapes all come from api-types.ts, which is generated from the server's OpenAPI document.
import type { components } from "./api-types";

type S = components["schemas"];

export type SwitchboardEvent = S["EventHistory"]["events"][number];
export type EventType = SwitchboardEvent["type"];
export type EventOf<T extends EventType> = Extract<SwitchboardEvent, { type: T }>;

export type Chain = S["Chain"];
export type Reason = S["Reason"];
export type IntentResponse = S["IntentResponse"];
export type Route = S["Route"];
export type Decision = S["Decision"];
export type ExecuteResponse = S["ExecuteResponse"];
export type StatsResponse = S["StatsResponse"];
export type ApprovalView = S["ApprovalView"];
export type OkResponse = S["OkResponse"];
export type Wallet = S["Wallet"];
export type Venue = S["Venue"];
export type MarketMatchResponse = S["MarketMatchResponse"];
export type MarketQuoteResponse = S["MarketQuoteResponse"];
export type MarketBrief = S["MarketBrief"];
export type MarketListing = S["MarketListing"];
export type MoveOption = S["MoveOption"];
export type Opinion = S["Opinion"];
export type TrackRecord = S["TrackRecord"];
export type ForecastList = S["ForecastList"];
export type RuleView = S["RuleView"];
export type RuleProposal = S["RuleProposal"];
export type HostileResult = S["HostileResult"];
export type DailySummary = S["DailySummary"];
export type BotNote = S["BotNote"];
export type ConnectorView = S["ConnectorView"];
export type ConnectorList = S["ConnectorList"];
export type ConnectorCreate = S["ConnectorCreate"];
export type ConnectorUpdate = S["ConnectorUpdate"];
export type ConnectorCreated = S["ConnectorCreated"];

export const EVENT_TYPES: EventType[] = [
  "transfer.sent",
  "transfer.arrived",
  "approval.requested",
  "approval.resolved",
  "blocked",
  "trade.posted",
  "trade.filled",
  "stream.stale",
  "frozen",
  "call.started",
  "call.ended",
  "policy.updated",
  "forecast.committed",
  "forecast.revealed",
];
