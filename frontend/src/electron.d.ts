export {}

declare global {
  interface Window {
    bidding?: {
      isDesktop?: boolean
      openBidFolder: (
        folder: string,
      ) => Promise<{ ok: boolean; error?: string }>
    }
  }
}
