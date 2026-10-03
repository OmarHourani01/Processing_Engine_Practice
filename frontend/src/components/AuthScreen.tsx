import { FormEvent, useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { api } from '../api';

export function AuthScreen({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const auth = useMutation({
    mutationFn: () => mode === 'login' ? api.login({ username, password }) : api.register({ username, password, email: email || undefined }),
    onSuccess: onAuthenticated,
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    auth.mutate();
  }

  return <main className="auth-shell">
    <section className="auth-art">
      <a className="brand brand-light" href="#home"><span className="brand-mark">F</span><span>Image Processing Engine<span className="brand-dot">.</span></span></a>
      <div className="auth-art-content"><div className="auth-overline">A CLEARER VIEW OF YOUR FIELDWORK</div><h1>Every image<br />has a place.</h1><p>Bring your image collections together. Read the details, see where they belong, and explore the bigger picture.</p>
        <div className="auth-map-art"><div className="contour contour-a"/><div className="contour contour-b"/><div className="contour contour-c"/><span className="map-pin pin-a">⌖</span><span className="map-pin pin-b">⌖</span><span className="map-pin pin-c">⌖</span><div className="map-art-label">FIELD COLLECTION / 01</div></div>
      </div>
      <div className="auth-footer"><span>Private by default</span><span>Designed for field work</span></div>
    </section>
    <section className="auth-form-side"><div className="auth-form-wrap">
      <div className="auth-mobile-brand"><span className="brand-mark">F</span><strong>Image Processing Engine<span className="brand-dot">.</span></strong></div>
      <div className="eyebrow">YOUR PRIVATE WORKSPACE</div><h2>{mode === 'login' ? 'Welcome back' : 'Create your account'}</h2><p className="auth-subtitle">{mode === 'login' ? 'Sign in to pick up where you left off.' : 'Start organizing your image collections.'}</p>
      <form onSubmit={submit} className="auth-form">
        <label>Username<input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required minLength={2} placeholder="Your username" /></label>
        {mode === 'register' && <label>Email <span className="optional">(optional)</span><input type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" /></label>}
        <label>Password<input autoComplete={mode === 'login' ? 'current-password' : 'new-password'} type="password" value={password} onChange={(event) => setPassword(event.target.value)} required minLength={8} placeholder="At least 8 characters" /></label>
        {auth.error && <div className="form-error" role="alert">{auth.error.message}</div>}
        <button className="button button-primary auth-submit" type="submit" disabled={auth.isPending}>{auth.isPending ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'} <span>→</span></button>
      </form>
      <p className="auth-switch">{mode === 'login' ? 'New to Image Processing Engine?' : 'Already have an account?'} <button onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); auth.reset(); }}>{mode === 'login' ? 'Create an account' : 'Sign in'}</button></p>
      <div className="auth-privacy"><span>◇</span> Your datasets are only visible to you.</div>
    </div></section>
  </main>;
}
