"""Check the official Antigravity SDK against the production Vertex project."""
import asyncio
from google.antigravity import Agent, LocalAgentConfig


async def main():
    for model in ('gemini-3.8-flash', 'gemini-3.5-flash'):
        config = LocalAgentConfig(model=model, vertex=True,
                                  project='project-fdadb4dd-cb77-4270-9cd',
                                  location='global')
        async with Agent(config) as agent:
            response = await agent.chat('Reply with OK.')
            if not (await response.text()).strip():
                raise RuntimeError(f'{model}: empty response')
            print(f'{model}: success', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
