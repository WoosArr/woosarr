What do we want?
A Docker container that can be loaded in unraid
Integrate with a plex server.
    Oauth authentication
        no hardcoding of Credentials or Tokens
Integrate with a discord bot.
A Web interface for configuration (port 2000)
    Sidebar navigation
Page to view plex users, and thier linked discord account, their discord roles, and plex libraries shared with them 
    with the ability to add/remove libraries manually to one or more users
Page to view roles of the discord server
    Ability to assign plex libraries to discord roles.
Page to view the Plex server and its libraries
    Ability to exempt a library from being added or removed when a role changes, maintaining its status
Automatic assignment/removal of libraries 
    when a discord role is added or removed
    when a role is modified
    Making sure to respect library exemptions
    Role libraries are additive, not most restrictive
        Pay attention to how Plex's API/SDK handles user library management to make sure that users don't lose library access when adding/removing roles 
    Only modify roles of Plex users that have a linked Discord account
Page to view logs for debugging
    Detailed logging
Secure management of sensitive data (including, but not limited to)
    Tokens
    Credentials
A nice user interface with both light and dark modes
    Options for colorblind mode
    Purple should be a featured color 
Setup assistants for:
    Discord bot creation
    Plex integration

references
https://developer.plex.tv/pms/
https://plexapi.dev/SDKs
https://github.com/LukasParke/plexapi-dev-docs/tree/main/sdks/python
https://github.com/LukasParke/plexapi-dev-docs/tree/main/sdks/typescript
https://github.com/LukasParke/plexapi-dev-docs/tree/main/sdks/go
https://docs.discord.com/developers/intro