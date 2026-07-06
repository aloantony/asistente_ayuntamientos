/** @type {import('next').NextConfig} */
const internalApiBaseUrl = (
  process.env.INTERNAL_API_BASE_URL ??
  process.env.NEXT_PUBLIC_API_BASE_URL ??
  "http://127.0.0.1:8000"
).replace(/\/$/, "");

const nextConfig = {
  output: "standalone",
  async rewrites() {
    return {
      afterFiles: [
        {
          source: "/admin/:path*",
          destination: `${internalApiBaseUrl}/admin/:path*`,
        },
        {
          source: "/agent-office/:path*",
          destination: `${internalApiBaseUrl}/agent-office/:path*`,
        },
        {
          source: "/assistant/:path*",
          destination: `${internalApiBaseUrl}/assistant/:path*`,
        },
        {
          source: "/auth/:path*",
          destination: `${internalApiBaseUrl}/auth/:path*`,
        },
        {
          source: "/documents/:path*",
          destination: `${internalApiBaseUrl}/documents/:path*`,
        },
        {
          source: "/geo/:path*",
          destination: `${internalApiBaseUrl}/geo/:path*`,
        },
        {
          source: "/health",
          destination: `${internalApiBaseUrl}/health`,
        },
        {
          source: "/municipalities/:path*",
          destination: `${internalApiBaseUrl}/municipalities/:path*`,
        },
        {
          source: "/ordinances/:path*",
          destination: `${internalApiBaseUrl}/ordinances/:path*`,
        },
        {
          source: "/organizations/:path*",
          destination: `${internalApiBaseUrl}/organizations/:path*`,
        },
        {
          source: "/projects/:path*",
          destination: `${internalApiBaseUrl}/projects/:path*`,
        },
        {
          source: "/requirements/:path*",
          destination: `${internalApiBaseUrl}/requirements/:path*`,
        },
        {
          source: "/telegram/:path*",
          destination: `${internalApiBaseUrl}/telegram/:path*`,
        },
      ],
    };
  },
};

module.exports = nextConfig;
