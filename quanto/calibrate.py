import torch
from torch.nn.modules.module import (
    register_module_forward_hook,
    register_module_forward_pre_hook,
)
from torch.overrides import TorchFunctionMode

from .nn import QModuleMixin
from .qtensor import QTensor, absmax_scale


__all__ = ["Calibration"]


def _updated_scale(scale, new_scale, momentum):
    if torch.all(scale == 1):
        return new_scale
    return momentum * scale + new_scale * (1.0 - momentum)


class Calibration(TorchFunctionMode):
    """A custom torch dispatch mode to calibrate quantized modules.

    In order to improve the accuracy of the quantized activations, the input and output
    scales of each quantized module is evaluated per-batch using the absmax algorithm and aggregated using a
    momentum.

    The dispatch mode also tracks the calls to each torch function down the model graph: eventually this will
     allow to optimize activations between quantized modules.

    Args:
        momentum (`float`): the momentum to use when updating scales.
    """

    def __init__(self, *args, momentum: float = 0.9, **kwargs):
        super().__init__(*args, **kwargs)
        self.momentum = momentum

    def __torch_function__(self, func, types, args=(), kwargs=None):
        kwargs = kwargs if kwargs is not None else {}
        return func(*args, **kwargs)

    def __enter__(self):
        super().__enter__()
        self.pre_handle = register_module_forward_pre_hook(self.calibrate_input)
        self.post_handle = register_module_forward_hook(self.calibrate_output)

    def __exit__(self, exc_type, exc_val, exc_tb):
        super().__exit__(exc_type, exc_val, exc_tb)
        self.pre_handle.remove()
        self.post_handle.remove()

    def calibrate_input(self, module: torch.nn.Module, input, momentum: float = 0.9):
        if isinstance(module, QModuleMixin) and module.activations is not None:
            input = input[0]
            if isinstance(input, QTensor):
                # Just adopt the maximum scale of the input
                module.input_scale = torch.max(input._scale)
            else:
                # Evaluate the best scale
                input_scale = absmax_scale(input, module.activations)
                module.input_scale = _updated_scale(module.input_scale, input_scale, momentum)
            return input

    def calibrate_output(
        self,
        module: torch.nn.Module,
        input,
        output,
    ):
        if isinstance(module, (QModuleMixin)) and module.activations is not None:
            # Reevaluate raw module output
            qoutput = module.qforward(input[0])
            if isinstance(qoutput, QTensor):
                qoutput = qoutput.dequantize()
            # Evaluate the optimal scale per-tensor and update output scale
            output_scale = absmax_scale(qoutput, module.activations, axis=None)
            module.output_scale = _updated_scale(module.output_scale, output_scale, self.momentum)
            # Reevaluate output with the correct output scale
            return module.forward(input[0])
