-- Stores hashing data for a user.

CREATE TABLE login_tokens ( 
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(), 
    email               citext NOT NULL, 
    token_hash          text NOT NULL UNIQUE,    
    expires_at          timestamptz NOT NULL ,
    used_at             timestamptz ,
    created_at          timestamptz NOT NULL DEFAULT now() 

);

COMMENT ON TABLE login_tokens IS 'a table to store the users hashed email and
not on their user in case they dont have an account already set up ';

COMMENT ON COLUMN login_tokens.used_at IS 'NULL means that the token is spendable. It notes when the user
clicks on the email link ';