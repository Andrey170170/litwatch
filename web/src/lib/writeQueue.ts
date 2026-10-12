/**
 * Writes to one thing, in the order they were made. A queue per key chains each job after
 * the previous one, so a reaction's PUT and DELETE, or a Calibrate item's answers, never
 * overlap and never land out of order. A failed job does not block the next.
 */
export class WriteQueue {
  private tails = new Map<string, Promise<unknown>>();

  enqueue<T>(key: string, job: () => Promise<T>): Promise<T> {
    const previous = this.tails.get(key) ?? Promise.resolve();
    const next = previous.then(job, job);
    this.tails.set(
      key,
      next.then(
        () => undefined,
        () => undefined,
      ),
    );
    return next;
  }
}

export const writes = new WriteQueue();
