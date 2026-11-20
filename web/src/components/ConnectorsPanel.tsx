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
        <div className="row">
          <button type="button" disabled={busy} onClick={() => setEditing((open) => !open)}>
            {editing ? "Close" : "Change limit"}
          </button>
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() => void run(() => api.revokeConnector(connector.id))}
          >
            Revoke
          </button>
        </div>
      </div>
      <p className="feed-detail">
        {limitText(connector)} · sent {money(connector.sent_today_usd)} today · asked you{" "}
        {connector.asks_today} time{connector.asks_today === 1 ? "" : "s"} today ·{" "}
        {connector.last_used ? `last used ${clock(connector.last_used)}` : "never used"}
      </p>
      {editing && (
        <form
          className="row"
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
            className="primary"
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
    <section className="panel" aria-labelledby="connectors-title">
      <h2 id="connectors-title">Connected assistants</h2>
      <p className="muted">
        Let ChatGPT, Claude, Grok or another assistant ask Switchboard to send. Each one gets its
        own key and its own limit. It can pay only people already in your wallet book. It cannot
        approve anything, change a limit or see your word.
      </p>
      {list && !list.public && (
        <p className="notice notice-warning">
          No public address is set, so only programs on this computer can reach Switchboard. To
          connect an assistant that runs elsewhere, open a tunnel and set PUBLIC_BASE_URL.
        </p>
      )}
      {list?.simulated && (
        <p className="muted">Transfers are simulated here; an assistant is told so.</p>
      )}

      {live.length === 0 ? (
        <p className="empty">No assistant is connected.</p>
      ) : (
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

      <form className="subpanel connector-form" onSubmit={(event) => void connect(event)}>
        <h3>Connect an assistant</h3>
        <div className="row">
          <label>
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
          <label>
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
          <label>
            How it connects
            <select value={auth} onChange={(event) => setAuth(event.target.value as Auth)}>
              <option value="link">Secret address (no key field needed)</option>
              <option value="header">Key in a header</option>
            </select>
          </label>
        </div>
        <div className="row">
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
            className="primary"
            disabled={busy || singleValue === null || dailyValue === null}
          >
            Connect
          </button>
        </div>
        <p className="muted">
          {singleValue === 0
            ? "At 0 it asks you about every transfer."
            : "Your vault's own limits still apply on top of these."}{" "}
          {list ? `An assistant can have you asked ${list.max_asks_per_day} times a day.` : ""}
        </p>
        {auth === "link" && (
          <p className="muted">
            With a secret address, anyone who has the address can act as this assistant, inside
            its limit. Keep the limit low, and revoke it if the address gets out.
          </p>
        )}
      </form>

      {created && (
        <div className="notice notice-good connector-created" role="status">
          <div>
            <p>
              <strong>{created.connector.name} is connected.</strong> This is shown once. Copy it
              now; Switchboard keeps only a fingerprint of it.
            </p>
            <Secret
              label={created.connector.auth === "link" ? "Secret address" : "Address"}
              value={created.mcp_url}
            />
            {created.connector.auth === "header" && <Secret label="Key" value={created.token} />}
            <p className="muted">{KINDS.find((item) => item.id === created.connector.kind)?.setup}</p>
            <button type="button" onClick={() => setCreated(null)}>
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
