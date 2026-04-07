from environs import Env

Env.thishasallmethods

We don't need to use the instance if env until we're loading .env files, at that point it'll be useful but until then we can just use the module level
