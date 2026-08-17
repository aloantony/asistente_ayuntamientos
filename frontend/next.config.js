function apiProxyTarget() {
  const configured = process.env.API_PROXY_TARGET ?? "http://127.0.0.1:8000";
  const parsed = new URL(configured);
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    (parsed.pathname !== "/" && parsed.pathname !== "")
  ) {
    throw new Error("API_PROXY_TARGET must be an HTTP(S) origin");
  }
  return parsed.origin;
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiProxyTarget()}/:path*`,
      },
    ];
  },
};

module.exports = nextConfig;
