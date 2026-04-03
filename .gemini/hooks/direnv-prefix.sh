#!/usr/bin/env bash
# Read the tool input from stdin
input=$(cat)

# Extract the original command
original_cmd=$(echo "$input" | jq -r '.tool_input.command')

# Check if the command already starts with 'direnv exec . '
if [[ "$original_cmd" == "direnv exec . "* ]]; then
    # Already prefixed, don't add it again
    new_cmd="$original_cmd"
else
    # Prepend direnv exec . to the command
    new_cmd="direnv exec . $original_cmd"
fi

# Build the JSON output using jq for safety
jq -n --arg cmd "$new_cmd" '{
  hookSpecificOutput: {
    tool_input: {
      command: $cmd
    }
  }
}'
