// Browser side of the passkey check. The server builds the options and verifies the result;
// this file only moves them through `navigator.credentials`.
import { ApiError } from "./api";
import type { OkResponse } from "./types";

type Json = Record<string, unknown>;

function fromBase64Url(value: string): ArrayBuffer {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes.buffer;
}

function toBase64Url(buffer: ArrayBuffer): string {
  let binary = "";
  for (const byte of new Uint8Array(buffer)) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = (await response.json()) as T & { detail?: string };
  if (!response.ok) {
    throw new ApiError(payload.detail ?? `The server answered ${response.status}.`, response.status);
  }
  return payload;
}

export function passkeysSupported(): boolean {
  return typeof PublicKeyCredential !== "undefined" && !!navigator.credentials;
}

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] {
  return ((list as { id: string; type: "public-key" }[] | undefined) ?? []).map((item) => ({
    ...item,
    id: fromBase64Url(item.id),
  }));
}

function credentialJson(credential: PublicKeyCredential): Json {
  const response = credential.response as AuthenticatorAttestationResponse &
    AuthenticatorAssertionResponse;
  const body: Json = { clientDataJSON: toBase64Url(response.clientDataJSON) };
  if (response.attestationObject) body.attestationObject = toBase64Url(response.attestationObject);
  if (response.authenticatorData) body.authenticatorData = toBase64Url(response.authenticatorData);
  if (response.signature) body.signature = toBase64Url(response.signature);
  if (response.userHandle) body.userHandle = toBase64Url(response.userHandle);
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: credential.type,
    response: body,
    clientExtensionResults: credential.getClientExtensionResults(),
  };
}

/** Register this device's passkey. Done once, before any large approval. */
export async function registerPasskey(): Promise<void> {
  const options = await post<Json>("/api/passkey/register/options", {});
  const user = options.user as { id: string; name: string; displayName: string };
  const credential = (await navigator.credentials.create({
    publicKey: {
      ...(options as unknown as PublicKeyCredentialCreationOptions),
      challenge: fromBase64Url(options.challenge as string),
      user: { ...user, id: fromBase64Url(user.id) },
      excludeCredentials: descriptors(options.excludeCredentials),
    },
  })) as PublicKeyCredential | null;
  if (!credential) throw new ApiError("The passkey prompt was dismissed.", 0);
  await post<OkResponse>("/api/passkey/register/verify", { credential: credentialJson(credential) });
}

/** Confirm one approval request with the registered passkey. */
export async function confirmWithPasskey(requestId: string): Promise<OkResponse> {
  const options = await post<Json>("/api/passkey/assert/options", { request_id: requestId });
  const credential = (await navigator.credentials.get({
    publicKey: {
      ...(options as unknown as PublicKeyCredentialRequestOptions),
      challenge: fromBase64Url(options.challenge as string),
      allowCredentials: descriptors(options.allowCredentials),
    },
  })) as PublicKeyCredential | null;
  if (!credential) throw new ApiError("The passkey prompt was dismissed.", 0);
  return post<OkResponse>("/api/passkey/assert/verify", {
    request_id: requestId,
    credential: credentialJson(credential),
  });
}
