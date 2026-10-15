-- Debug mode: the text a model replied when the reply could not be used (invalid, truncated or
-- refused). NULL out of debug mode, for a usable reply, and for a call that got no reply.
ALTER TABLE inference_calls ADD COLUMN reply text;
