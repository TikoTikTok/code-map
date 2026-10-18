import type { User, Id } from './types';

export const BASE = 'https://example.test';

/** Fetch one user. */
export async function fetchUser(id: Id): Promise<User> {
  const res = await fetch(`${BASE}/users/${id}`);
  return res.json();
}

export const toName = (u: User) => u.name;
