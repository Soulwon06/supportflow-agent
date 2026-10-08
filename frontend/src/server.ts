import { env } from "cloudflare:workers";
import handler, { createServerEntry } from "@tanstack/react-start/server-entry";

type WorkerEnv = {
  BACKEND_ORIGIN?: string;
};

function backendTarget(request: Request, origin: string): URL {
  const incoming = new URL(request.url);
  const path = incoming.pathname.replace(/^\/api/, "") || "/";
  return new URL(`${path}${incoming.search}`, origin.replace(/\/$/, ""));
}

export default createServerEntry({
  async fetch(request, requestOptions) {
    const incoming = new URL(request.url);

    if (incoming.pathname.startsWith("/api/")) {
      const backendOrigin = (env as WorkerEnv).BACKEND_ORIGIN;
      if (!backendOrigin) {
        return new Response("BACKEND_ORIGIN is not configured", { status: 503 });
      }
      const target = backendTarget(request, backendOrigin);
      const headers = new Headers(request.headers);
      headers.delete("host");
      const body = ["GET", "HEAD"].includes(request.method) ? undefined : request.body;
      return fetch(new Request(target, {
        method: request.method,
        headers,
        body,
        redirect: "manual",
      }));
    }

    return handler.fetch(request, requestOptions);
  },
});
