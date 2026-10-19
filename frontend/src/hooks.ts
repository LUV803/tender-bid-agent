import { useCallback, useEffect, useState } from 'react'
import type { JobAccepted, JobResponse, HealthInfo } from './api'
import { errorMessage, getHealth, getJob } from './api'

const POLL_INTERVAL = 1500

export function isTerminalJob(job: JobResponse | null): boolean {
  return job?.status === 'succeeded' || job?.status === 'failed'
}

export function useJobPolling(initial: JobAccepted | null): {
  job: JobResponse | null
  error: string | null
} {
  const [job, setJob] = useState<JobResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const jobId = initial?.job_id ?? null

  useEffect(() => {
    setJob(null)
    setError(null)
    if (!jobId) return
    let cancelled = false
    let timer: number | undefined

    const tick = async () => {
      try {
        const next = await getJob(jobId)
        if (cancelled) return
        setJob(next)
        setError(null)
        if (next.status !== 'succeeded' && next.status !== 'failed') {
          timer = window.setTimeout(tick, POLL_INTERVAL)
        }
      } catch (reason) {
        if (cancelled) return
        setError(errorMessage(reason))
        timer = window.setTimeout(tick, POLL_INTERVAL * 2)
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [jobId])

  return { job, error }
}

export function useHealth(): {
  health: HealthInfo | null
  online: boolean | null
} {
  const [health, setHealth] = useState<HealthInfo | null>(null)
  const [online, setOnline] = useState<boolean | null>(null)

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined

    const check = async () => {
      try {
        const info = await getHealth()
        if (cancelled) return
        setHealth(info)
        setOnline(true)
      } catch {
        if (cancelled) return
        setOnline(false)
      }
      timer = window.setTimeout(check, 15000)
    }

    void check()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [])

  return { health, online }
}

export function useApiData<T>(
  load: () => Promise<T>,
  refreshToken: string | number = 0,
): {
  data: T | null
  loading: boolean
  error: string | null
  refresh: () => void
} {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const refresh = useCallback(() => {
    setVersion((current) => current + 1)
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    load()
      .then((value) => {
        if (cancelled) return
        setData(value)
        setError(null)
      })
      .catch((reason: unknown) => {
        if (cancelled) return
        setError(errorMessage(reason))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [load, refreshToken, version])

  return { data, loading, error, refresh }
}
