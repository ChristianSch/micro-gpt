"""
MiniAGI main executable.
This script serves as the main entry point for the MiniAGI application. It provides a command-line
interface for users to interact with a GPT-3.5/4 language model, leveraging memory management and
context-based reasoning to achieve user-defined objectives. The agent can issue various types of
commands, such as executing Python code, running shell commands, reading files, searching the web,
scraping websites, and conversing with users.
"""

# pylint: disable=invalid-name, broad-exception-caught, exec-used, unspecified-encoding, wrong-import-position, import-error

import os
import sys
import re
import subprocess
import platform
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
from urllib.request import urlopen
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from termcolor import colored
import openai
from duckduckgo_search import ddg
from thinkgpt.llm import ThinkGPT
from spinner import Spinner


operating_system = platform.platform()

def get_bool(env_var: str) -> bool:
    '''
    Gets the value of a boolean environment variable.
    Args:
        env_var (str): Name of the variable
    '''
    return os.getenv(env_var) in ['true', '1', 't', 'y', 'yes']

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
DEBUG = get_bool("DEBUG")
ENABLE_CRITIC = get_bool("ENABLE_CRITIC")
PROMPT_USER = get_bool("PROMPT_USER")

PROMPT = f"You are an autonomous agent running on {operating_system}." + '''
OBJECTIVE: {objective} (e.g. "Find a recipe for chocolate chip cookies")

You are working towards the objective on a step-by-step basis. Previous steps:

{context}

Your task is to respond with the next action.
Supported commands are: execute_python, execute_shell, read_file, web_search, web_scrape, talk_to_user, or done
The mandatory action format is:

<r>[YOUR_REASONING]</r><c>[COMMAND]</c>
[ARGUMENT]

ARGUMENT may have multiple lines if the argument is Python code.
Use only non-interactive shell commands.
web_scrape argument must be a single URL.
Python code run with execute_python must end with an output "print" statement and should be well-commented.
Send the "done" command if the objective was achieved in a previous command or if no further action is required.
RESPOND WITH PRECISELY ONE THOUGHT/COMMAND/ARG COMBINATION.
DO NOT CHAIN MULTIPLE COMMANDS.
DO NOT INCLUDE EXTRA TEXT BEFORE OR AFTER THE COMMAND.
DO NOT REPEAT PREVIOUSLY EXECUTED COMMANDS.

Example actions:

<r>Search for websites relevant to chocolate chip cookies recipe.</r><c>web_search</c>
chocolate chip cookies recipe

<r>Scrape information about chocolate chip cookies from the given URL.</r><c>web_scrape</c>
https://example.com/chocolate-chip-cookies

<r>I need to ask the user for guidance.</r><c>talk_to_user</c>
What is the URL of a website with chocolate chip cookies recipes?

<r>Write 'Hello, world!' to file</r><c>execute_python</c>
# Opening file in write mode and writing 'Hello, world!' into it
with open('hello_world.txt', 'w') as f:
    f.write('Hello, world!')

<r>The objective is complete.</r><c>done</c>
'''

SUMMARY_HINT = "Do your best to retain information necessary for the agent to perform its task."
EXTRA_SUMMARY_HINT = "If the text contains information related to the topic: '{summarizer_hint}'"\
    "then include it. If not, write a standard summary."


def update_memory(
        _agent: ThinkGPT,
        _action: str,
        _observation: str,
        previous_summary: str
    ) -> str:

    new_memory = f"ACTION\n{_action}\nRESULT:\n{_observation}\n"

    new_summary = summarizer.summarize(
        f"{previous_summary}\n{new_memory}", max_memory_item_size,
        instruction_hint="Generate a new summary given the previous summary"\
            "of the agent's history and its last action. Be concise, use abbreviations."
        )

    _agent.memorize(new_memory)

    return new_summary


if __name__ == "__main__":

    agent = ThinkGPT(
        model_name=os.getenv("MODEL"),
        request_timeout=600,
        verbose=False
        )

    summarizer = ThinkGPT(
        model_name=os.getenv("SUMMARIZER_MODEL"),
        request_timeout=600,
        verbose=False
        )

    if len(sys.argv) != 2:
        print("Usage: miniagi.py <objective>")
        sys.exit(0)

    objective = sys.argv[1]
    max_context_size = int(os.getenv("MAX_CONTEXT_SIZE"))
    max_memory_item_size = int(os.getenv("MAX_MEMORY_ITEM_SIZE"))
    context = objective
    thought = ""
    observation = ""
    summarized_history = ""

    work_dir = os.getenv("WORK_DIR")

    if work_dir is None or not work_dir:
        work_dir = os.path.join(Path.home(), "miniagi")
        if not os.path.exists(work_dir):
            os.makedirs(work_dir)

    print(f"Working directory is {work_dir}")

    try:
        os.chdir(work_dir)
    except FileNotFoundError:
        print("Directory doesn't exist. Set WORK_DIR to an existing directory or leave it blank.")
        sys.exit(0)

    while True:

        action_buffer = "\n".join(
                    agent.remember(
                    limit=32,
                    sort_by_order=True,
                    max_tokens=max_context_size
                )
        )

        context = f"HISTORY\n{summarized_history}\nPREV ACTIONS:\n{action_buffer}"

        if DEBUG:
            print(f"CONTEXT:\n{context}")

        with Spinner():

            try:
                response_text = agent.predict(
                    prompt=PROMPT.format(context=context, objective=objective)
                )

            except openai.error.InvalidRequestError as e:
                if 'gpt-4' in str(e):
                    print("Prompting the gpt-4 model failed. Falling back to gpt-3.5-turbo")
                    agent = ThinkGPT(model_name='gpt-3.5-turbo', verbose=False)
                    continue
                print("Error accessing the OpenAI API: " + str(e))
                sys.exit(0)

        if DEBUG:
            print(f"RAW RESPONSE:\n{response_text}")

        res_lines = response_text.split("\n")

        try:
            PATTERN = r'<(r|c)>(.*?)</(r|c)>'
            matches = re.findall(PATTERN, res_lines[0])

            thought = matches[0][1]
            command = matches[1][1]

            if command == "done":
                print(colored(f"The agent concluded: {thought}", "cyan"))
                sys.exit(0)

            # Account for GPT-3.5 sometimes including an extra "done"
            if "done" in res_lines[-1]:
                res_line = res_lines[:-1]

            arg = "\n".join(res_lines[1:])

            # Remove unwanted code formatting backticks
            arg = arg.replace("```", "")

        except Exception as e:
            print(colored("Unable to parse response. Retrying...\n", "red"))
            observation = "This command was formatted"\
                " incorrectly. Use the correct syntax using the <r> and <c> tags."
            update_memory(agent, res_lines[0], observation, summarized_history)
            continue

        _arg = arg.replace("\n", "\\n") if len(arg) < 64 else f"{arg[:64]}...".replace("\n", "\\n")
        action = f"{thought}\nCmd: {command}, Arg: \"{arg}\""
        abbreviated_action = f"{thought}\nCmd: {command}, Arg: \"{_arg}\""

        if command == "talk_to_user":
            print(colored(f"MiniAGI: {arg}", 'cyan'))
            user_input = input('Your response: ')
            observation = f"The user responded with: {user_input}."
            update_memory(agent, abbreviated_action, observation, summarized_history)
            continue

        print(colored(f"MiniAGI: {abbreviated_action}", "cyan"))

        if PROMPT_USER:
            user_input = input('Press enter to perform this action or abort by typing feedback: ')

            if len(user_input) > 0:
                observation = "The user responded with: {user_input}\n"\
                    "Take this comment into consideration."
                update_memory(agent, abbreviated_action, observation, summarized_history)
                continue

        try:
            if command == "execute_python":
                _stdout = StringIO()
                with redirect_stdout(_stdout):
                    exec(arg)
                observation = _stdout.getvalue()
            elif command == "execute_shell":
                result = subprocess.run(arg, capture_output=True, shell=True, check=False)

                stdout = result.stdout.decode("utf-8")
                stderr = result.stderr.decode("utf-8")

                if len(stderr) > 0:
                    print(colored(f"Execution error: {stderr}", "red"))
                observation = f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            elif command == "web_search":
                print("SEARCH WEB")
                observation = ddg(arg, max_results=5)
                print(observation)
                if observation is None:
                    print("SEARCH FAILED")
            elif command == "web_scrape":
                with urlopen(arg) as response:
                    html = response.read()

                with Spinner():
                    response_text = summarizer.chunked_summarize(
                        content=BeautifulSoup(
                            html,
                            features="lxml"
                        ).get_text(),
                        max_tokens=max_memory_item_size,
                        instruction_hint=SUMMARY_HINT +
                            EXTRA_SUMMARY_HINT.format(summarizer_hint=objective)
                    )

                observation = response_text
            elif command == "read_file":
                with Spinner():
                    with open(arg, "r") as f:
                        file_content = summarizer.chunked_summarize(
                            f.read(), max_memory_item_size,
                            instruction_hint=SUMMARY_HINT +
                                EXTRA_SUMMARY_HINT.format(summarizer_hint=objective))
                observation = file_content
            elif command == "done":
                print("Objective achieved.")
                sys.exit(0)

            print("OBSERVATION: " + observation)

            update_memory(agent, action, observation, summarized_history)

        except Exception as e:
            if "context length" in str(e):
                print(colored(
                        f"{str(e)}\nTry decreasing MAX_CONTEXT_SIZE, MAX_MEMORY_ITEM_SIZE" \
                            " and SUMMARIZER_CHUNK_SIZE.",
                        "red"
                    )
                )

            print(colored(f"Execution error: {str(e)}", "red"))
            observation = f"The command returned an error:\n{str(e)}\n"
            update_memory(agent, action, observation, summarized_history)
