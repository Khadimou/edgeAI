import axios from "axios";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export const api = axios.create({
  baseURL: `${API_URL}/api/v1`,
  headers: { "Content-Type": "application/json" },
});

api.interceptors.request.use((config) => {
  if (typeof window !== "undefined") {
    const token = localStorage.getItem("access_token");
    if (token) config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

api.interceptors.response.use(
  (res) => res,
  async (error) => {
    const original = error.config;
    if (error.response?.status === 401 && !original._retry) {
      original._retry = true;
      try {
        const refresh = localStorage.getItem("refresh_token");
        if (!refresh) throw new Error("No refresh token");
        const { data } = await axios.post(`${API_URL}/api/v1/auth/refresh`, {
          refresh_token: refresh,
        });
        localStorage.setItem("access_token", data.access_token);
        localStorage.setItem("refresh_token", data.refresh_token);
        original.headers.Authorization = `Bearer ${data.access_token}`;
        return api(original);
      } catch {
        localStorage.clear();
        window.location.href = "/login";
      }
    }
    return Promise.reject(error);
  }
);

// Auth
export const authApi = {
  register: (email: string, password: string, name?: string) =>
    api.post("/auth/register", { email, password, name }),
  login: (email: string, password: string) =>
    api.post("/auth/login", { email, password }),
  me: (accessToken?: string) =>
    api.get("/user/me", accessToken ? { headers: { Authorization: `Bearer ${accessToken}` } } : undefined),
  updateProfile: (data: Record<string, unknown>) => api.post("/user/profile", data),
};

// Matches
export const matchesApi = {
  upcoming: (league?: string, limit?: number) =>
    api.get("/matches/upcoming", { params: { league, limit } }),
  analysis: (matchId: string) =>
    api.get(`/matches/${matchId}/analysis`),
};

// Today (multi-sport)
export const todayApi = {
  get: (sport: "FOOTBALL" | "NBA" = "FOOTBALL") =>
    api.get("/today", { params: { sport } }),
};

// Recommendations
export const recsApi = {
  list: (limit?: number) =>
    api.get("/recommendations/", { params: { limit } }),
  preview: () => api.get("/recommendations/preview"),
};

// Bets
export const betsApi = {
  create: (data: Record<string, unknown>) => api.post("/bets/", data),
  updateResult: (betId: string, data: Record<string, unknown>) =>
    api.patch(`/bets/${betId}/result`, data),
  list: (status?: string) => api.get("/bets/", { params: { status } }),
};

// Bankroll
export const bankrollApi = {
  history: () => api.get("/bankroll/history"),
};

// Stats
export const statsApi = {
  performance: () => api.get("/stats/performance"),
};

// Model performance (live monitoring)
export const modelApi = {
  performance: (days: number = 30) =>
    api.get("/model/performance", { params: { days } }),
};

// Backtest
export type BacktestMarket = "FOOTBALL_1X2" | "FOOTBALL_OU" | "FOOTBALL_AH" | "NBA" | "NBA_TOTALS";
export const backtestApi = {
  latest: (market: BacktestMarket = "FOOTBALL_1X2") =>
    api.get("/backtest/latest", { params: { market } }),
};

// Live tracking
export type TrackingMarket = "ALL" | "FOOTBALL_1X2" | "FOOTBALL_OU" | "FOOTBALL_AH" | "NBA";
export const trackingApi = {
  live: (days: number = 60, market: TrackingMarket = "ALL") =>
    api.get("/tracking/live", { params: { days, market } }),
};

// Admin observability
export const adminApi = {
  observability: () => api.get("/admin/observability"),
  explain: (matchId: string) => api.get(`/admin/explain/${matchId}`),
};

export const chatApi = {
  status: () => api.get("/chat/status"),
  message: (message: string, history: { role: "user" | "assistant"; content: string }[]) =>
    api.post("/chat/message", { message, history }),
};
