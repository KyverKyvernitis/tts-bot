import type { Express, RequestHandler, Response } from "express";

export function sendNoStoreJson(res: Response, payload: unknown) {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate, proxy-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("Expires", "0");
  res.json(payload);
}

export function registerGetPost(app: Express, paths: readonly string[], handler: RequestHandler) {
  for (const path of paths) {
    app.get(path, handler);
    app.post(path, handler);
  }
}

export function registerGetOnly(app: Express, paths: readonly string[], handler: RequestHandler) {
  for (const path of paths) {
    app.get(path, handler);
  }
}

export function registerPostOnly(app: Express, paths: readonly string[], handler: RequestHandler) {
  for (const path of paths) {
    app.post(path, handler);
  }
}
