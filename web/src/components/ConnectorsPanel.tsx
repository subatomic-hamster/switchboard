import { type FormEvent, useCallback, useEffect, useState } from "react";

import { ApiError, api } from "../api";
import { clock, money } from "../format";
import type { ConnectorCreated, ConnectorList, ConnectorView } from "../types";

type Kind = ConnectorView["kind"];
type Auth = ConnectorView["auth"];

const KINDS: { id: Kind; label: string; auth: Auth; setup: string }[] = [
  {
    id: "chatgpt",
    label: "ChatGPT",
    auth: "link",
    setup:
      "In ChatGPT, turn on developer mode, add a custom MCP server, choose no authentication and paste the address. ChatGPT has no field for a key, so the address is the secret.",
  },
  {
    id: "claude",
    label: "Claude",
    auth: "link",
    setup:
      "In Claude, add a custom connector, choose no sign-in and paste the address. In Claude Code or the API, use the key type instead and send it as a bearer token.",
  },
  {
    id: "grok",
    label: "Grok",
    auth: "link",
    setup:
      "At grok.com/connectors, add a custom connector with the address. Through the xAI API, use the key type and pass it as the authorization value.",
  },
  {
    id: "gemini",
    label: "Gemini",
    auth: "header",
    setup:
      "Gemini CLI and the Gemini API take the address with the key as an Authorization header. The Gemini app asks connectors to sign in with OAuth, which Switchboard does not offer yet.",
  },
  {
    id: "meta",
    label: "Meta AI",
    auth: "link",
    setup:
      "Meta AI's connectors are a developer preview. What its app accepts is not confirmed; try the address.",
  },
  {
    id: "other",
    label: "Another assistant",
    auth: "header",
    setup:
      "Any MCP client: the address, with the key sent as Authorization: Bearer. Without MCP, use the JSON routes listed in the README.",
  },
];

const KIND_LABEL = Object.fromEntries(KINDS.map((kind) => [kind.id, kind.label])) as Record<
  Kind,
  string
>;

function limitText(connector: ConnectorView): string {
  if (connector.auto_send_usd === 0) return "Asks you every time";
  return `Sends up to ${money(connector.auto_send_usd)} without asking, ${money(connector.daily_usd)} a day`;
}

function amount(text: string): number | null {
  const value = Number(text);
  return text.trim() !== "" && Number.isFinite(value) && value >= 0 ? value : null;
}

function Secret({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <p className="secret">
      <span className="word-caption">{label}</span>
      <code data-testid="connector-secret">{value}</code>
      <button
        type="button"
        className="chip"
        onClick={() => {
          void navigator.clipboard?.writeText(value).then(() => setCopied(true));
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </p>
  );
}

function ConnectorRow({
  connector,
  onChanged,
}: {
  connector: ConnectorView;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [single, setSingle] = useState(String(connector.auto_send_usd));
  const [daily, setDaily] = useState(String(connector.daily_usd));
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setMessage("");
    try {
      await action();
      setEditing(false);
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  const singleValue = amount(single);
  const dailyValue = amount(daily);

  return (
    <li className="connector">
      <div className="connector-head">
        <p>
          <strong>{connector.name}</strong>{" "}
          <span className="muted">
            {connector.name === KIND_LABEL[connector.kind] ? "" : `${KIND_LABEL[connector.kind]} · `}
            {connector.auth === "link" ? "secret address" : "key"} ending {connector.token_hint}
          </span>
        </p>
        <div className="actions">
          <button type="button" className="btn" disabled={busy} onClick={() => setEditing((open) => !open)}>
            {editing ? "Close" : "Change limit"}
          </button>
          <button
            type="button"
            className="btn btn-danger"
            disabled={busy}
            onClick={() => void run(() => api.revokeConnector(connector.id))}
          >
            Revoke
          </button>
        </div>
      </div>
      <p
        className="row-detail"
        title={connector.last_used ? `Last used ${clock(connector.last_used)}` : "Never used"}
      >
        {limitText(connector)} · sent {money(connector.sent_today_usd)} today · asked you{" "}
        {connector.asks_today} time{connector.asks_today === 1 ? "" : "s"} today
      </p>
      {editing && (
        <form
          className="field-row"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            if (singleValue === null || dailyValue === null) return;
            void run(() =>
              api.updateConnector(connector.id, {
                auto_send_usd: singleValue,
                daily_usd: dailyValue,
              }),
            );
          }}
        >
          <label>
            Without asking, up to
            <input
              type="number"
              min={0}
              step="0.01"
              value={single}
              onChange={(event) => setSingle(event.target.value)}
            />
          </label>
          <label>
            In a day, up to
            <input
              type="number"
              min={0}
              step="0.01"
              value={daily}
              onChange={(event) => setDaily(event.target.value)}
            />
          </label>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={busy || singleValue === null || dailyValue === null}
          >
            Save limit
          </button>
        </form>
      )}
      {message && (
        <p role="status" className="muted">
          {message}
        </p>
      )}
    </li>
  );
}

/** Give outside assistants a key to Switchboard, each inside its own limit. */
export function ConnectorsPanel({ refreshKey }: { refreshKey: number }) {
  const [list, setList] = useState<ConnectorList | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<Kind>("chatgpt");
  const [auth, setAuth] = useState<Auth>("link");
  const [single, setSingle] = useState("0");
  const [daily, setDaily] = useState("0");
  const [created, setCreated] = useState<ConnectorCreated | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.connectors().then(setList).catch(() => undefined);
  }, []);
  useEffect(load, [load, refreshKey]);

  const chosenLabel = KIND_LABEL[kind];
  const singleValue = amount(single);
  const dailyValue = amount(daily);

  async function connect(event: FormEvent) {
    event.preventDefault();
    if (singleValue === null || dailyValue === null) return;
    setBusy(true);
    setMessage("");
    setCreated(null);
    try {
      setCreated(
        await api.createConnector({
          name: name.trim() || chosenLabel,
          kind,
          auth,
          auto_send_usd: singleValue,
          daily_usd: dailyValue,
        }),
      );
      setName("");
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : "Something went wrong.");
    } finally {
      setBusy(false);
      load();
    }
  }

  const live = list?.connectors.filter((connector) => !connector.revoked) ?? [];
  const revoked = (list?.connectors.length ?? 0) - live.length;

  return (
    <section className="card" aria-labelledby="connectors-title">
      <div className="card-head">
        <h2 id="connectors-title">Assistants</h2>
        <span className="muted">
          {live.length === 0 ? "None connected" : `${live.length} connected`}
          {list?.simulated ? " · simulated transfers" : ""}
        </span>
      </div>
      {list && !list.public && (
        <p className="note note-warning">
          No public address is set, so only this computer can connect. Set PUBLIC_BASE_URL and open a
          tunnel for others.
        </p>
      )}

      {live.length > 0 && (
        <ul className="connectors">
          {live.map((connector) => (
            <ConnectorRow key={connector.id} connector={connector} onChanged={load} />
          ))}
        </ul>
      )}
      {revoked > 0 && (
        <p className="muted">
          {revoked} revoked connector{revoked === 1 ? "" : "s"} not shown.
        </p>
      )}

      <form className="connector-form" onSubmit={(event) => void connect(event)}>
        <div className="field-row">
          <label className="field-label">
            Assistant
            <select
              value={kind}
              onChange={(event) => {
                const next = event.target.value as Kind;
                setKind(next);
                setAuth(KINDS.find((item) => item.id === next)?.auth ?? "header");
              }}
            >
              {KINDS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field-label">
            No-ask limit
            <input
              type="number"
              min={0}
              step="0.01"
              value={single}
              onChange={(event) => setSingle(event.target.value)}
            />
          </label>
          <label className="field-label">
            Per day
            <input
              type="number"
              min={0}
              step="0.01"
              value={daily}
              onChange={(event) => setDaily(event.target.value)}
            />
          </label>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={busy || singleValue === null || dailyValue === null}
          >
            Connect
          </button>
        </div>
        <p className="muted">
          Assistants can only pay people in your wallet book, and can't change limits.
        </p>
        <details className="fold">
          <summary>More options</summary>
          <div className="field-row">
            <label className="field-label">
              Name on the approval call
              <input
                type="text"
                value={name}
                placeholder={chosenLabel}
                maxLength={40}
                autoComplete="off"
                onChange={(event) => setName(event.target.value)}
              />
            </label>
            <label className="field-label">
              How it connects
              <select value={auth} onChange={(event) => setAuth(event.target.value as Auth)}>
                <option value="link">Secret address (no key field needed)</option>
                <option value="header">Key in a header</option>
              </select>
            </label>
          </div>
          <p className="muted">
            {singleValue === 0
              ? "At 0 it asks you about every transfer."
              : "Your vault's own limits still apply on top of these."}{" "}
            {list ? `An assistant can have you asked ${list.max_asks_per_day} times a day.` : ""}
          </p>
        </details>
      </form>

      {created && (
        <div className="note note-good connector-created" role="status">
          <div>
            <p>
              <strong>{created.connector.name} is connected.</strong> Copy it now; it is shown
              once.
            </p>
            <Secret
              label={created.connector.auth === "link" ? "Secret address" : "Address"}
              value={created.mcp_url}
            />
            {created.connector.auth === "header" && <Secret label="Key" value={created.token} />}
            {created.connector.auth === "link" && (
              <p className="muted">
                Anyone who has the address can act as this assistant, inside its limit. Revoke it if
                the address gets out.
              </p>
            )}
            <details className="fold">
              <summary>How to add it</summary>
              <p className="muted">
                {KINDS.find((item) => item.id === created.connector.kind)?.setup}
              </p>
            </details>
            <button type="button" className="btn" onClick={() => setCreated(null)}>
              I have copied it
            </button>
          </div>
        </div>
      )}
      {message && (
        <p role="status" className="muted">
          {message}
        </p>
      )}
    </section>
  );
}
