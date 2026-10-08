interface Env {
  BACKEND_ORIGIN?: string;
}

declare module "cloudflare:workers" {
  export const env: Env;
}
