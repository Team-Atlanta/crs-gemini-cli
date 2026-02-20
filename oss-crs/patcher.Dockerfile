# =============================================================================
# crs-gemini-cli Patcher Module
# =============================================================================
# RUN phase: Receives POVs, generates patches using Gemini CLI,
# tests them using the snapshot image for incremental rebuilds.
#
# Uses host Docker socket (mounted by framework) to access snapshot images.
# =============================================================================

# These ARGs are required by the oss-crs framework template
ARG target_base_image
ARG crs_version

FROM gemini-cli-base

# Install libCRS (CLI + Python package)
COPY --from=libcrs . /libCRS
RUN pip3 install /libCRS \
    && python3 -c "from libCRS.base import DataType; print('libCRS OK')"

# Install crs-gemini-cli package (patcher + agents)
COPY pyproject.toml /opt/crs-gemini-cli/pyproject.toml
COPY patcher.py /opt/crs-gemini-cli/patcher.py
COPY agents/ /opt/crs-gemini-cli/agents/
RUN pip3 install /opt/crs-gemini-cli

CMD ["run_patcher"]
