from pynq import MMIO
from pynq import Bitstream
from . import xlnk
from . import general_const
import time
import os
import math
import cffi
from subprocess import Popen, PIPE
import numpy as np

class FGPU:
    def __init__(self):
        """Class to use the FGPU architecture in iPython
        
        FGPU is a soft GPU architecture for FPGAs. It can be programmed
        using OpenCL. This class offers:
        * Compilation of OpenCL kernels
        * Binding memory regions to kernel parameters
        * Download tasks along with their binaries and execute them
        """
        
        self.bitfile = "" # bit file to program
        self.params = {}; # array to store the HW physical addresses of kernel parameters
        
        #initialize MMIO object
        self.base_addr = 0x43C00000 # 1st HW address of FGPU
        self.addr_space = 0x10000 # size of FGPU address space 
        self.status_reg_offset = 0x8000 # HW address of a control register 
        self.start_reg_offset = 0x8004 # HW address of a control register
        self.clean_cache_reg_offset = 0x8008 # HW address of a control register
        self.initiate_reg_offset = 0x800C # HW address of a control register
        self.mmio = MMIO(self.base_addr, self.addr_space)# map the control regsiter address space
        
        #initialize kernel descriptor
        self.kdesc = {
            #basic parameters (to be set by the user)
            'size0' : 0, # size of index space in 1st dimension
            'size1' : 0, # size of index space in 2nd dimension
            'size2' : 0, # size of index space in 3rd dimension
            'offset0' : 0, # offset of kernel index space in 1st dimension
            'offset1' : 0, # offset of kernel index space in 2nd dimension
            'offset2' : 0, # offset of kernel index space in 3rd dimension
            'wg_size0' : 0, # work-group size in 1st dimension
            'wg_size1' : 0, # work-group size in 2nd dimension
            'wg_size2' : 0, # work-group size in 3rd dimension
            'nParams' : 0, # number of kernel parameters
            'nDim' : 0, # number of activated dimensions in kernel index space
            
            #calculated parameters (computed according to user input)
            'size' : 0, # number of work-items to be launched
            'n_wg0' : 0, # number of work-groups to launch in 1st dimension 
            'n_wg1' : 0, # number of work-groups to launch in 2nd dimension 
            'n_wg2' : 0, # number of work-groups to launch in 3rd dimension 
            'wg_size' : 0, # number of work-items in a work-group
            'nWF_WG' : 0, # number of wavefronts in a work-group
            'start_addr' : 0 # address of the first instruction to be executed in CRAM
        }
        
        # file name that contains kernel code 
        self.kernelFileName = ""
        #kernel code
        self.kernel_code = []

    def __set_fclk0(self):
        """This internal method sets the output frequency of FCLK0 to 50MHz. 
        
        This has to be performed for any FGPU v2 overlay or bitstream
        """
        self._SCLR_BASE = 0xf8000000
        self._FCLK0_OFFSET = 0x170
        addr = self._SCLR_BASE + self._FCLK0_OFFSET
        FPGA0_CLK_CTRL = MMIO(addr).read()
        if FPGA0_CLK_CTRL != 0x300a00:
            #divider 0
            shift = 20
            mask = 0xF00000
            value = 0x3
            self.__set_regfield_value(addr, shift, mask, value)
            #divider 1
            shift = 8
            mask = 0xF00
            value = 0xa
            self.__set_regfield_value(addr, shift, mask, value)


    def __set_regfield_value(self, addr, shift, mask, value):
        
        curval = MMIO(addr).read()
        MMIO(addr).write(0, ((curval & ~mask) | (value << shift)))

    def download_bitstream(self):
        """Set the clock frequency and download the bitstream
        
        Parameters
        ----------
        None
        
        Returns
        -------
        None
        """
        self.__set_fclk0()
        Bitstream(self.bitfile).download()

    def download_kernel_code(self):
        """Download the binary to the compiled kernel into the CRAM (Code RAM) of FGPU
        
        Parameters
        ----------
        None
        
        Returns
        -------
        None
        """
        #kernel code memory offset
        kc_offset = 0x4000
        #copy instructions into sequential memory
        for i_offset, instruction in enumerate(self.kernel_code):
            self.mmio.write(kc_offset+i_offset*4, instruction)

    def prepare_kernel_descriptor(self):
        """Compute the settings for the kernel to be executed according to user input.
        For example, it computes the number of wavefronts within a work-group and
        the number of work-groups to be launched.
        
        Parameters
        ----------
        None
        
        Returns
        -------
        None
        """
        if self.kdesc['nDim'] == 1:
            self.kdesc['wg_size'] = self.kdesc['wg_size0']
            self.kdesc['size'] = self.kdesc['size0']
            self.kdesc['wg_size1'] = self.kdesc['wg_size2'] = 1
            self.kdesc['size1'] = self.kdesc['size2'] = 1
        elif self.kdesc['nDim'] == 2:
            self.kdesc['size'] = self.kdesc['size0'] * self.kdesc['size1']
            self.kdesc['wg_size'] = self.kdesc['wg_size0'] * self.kdesc['wg_size1']
            self.kdesc['wg_size2'] = 1
            self.kdesc['size2'] = 1
        
        self.kdesc['size'] = self.kdesc['size0'] * self.kdesc['size1'] * self.kdesc['size2']
        self.kdesc['wg_size'] = self.kdesc['wg_size0'] * self.kdesc['wg_size1'] * self.kdesc['wg_size2']
            
        self.kdesc['n_wg0'] = math.ceil(self.kdesc['size0'] / self.kdesc['wg_size0'])
        self.kdesc['n_wg1'] = math.ceil(self.kdesc['size1'] / self.kdesc['wg_size1'])
        self.kdesc['n_wg2'] = math.ceil(self.kdesc['size2'] / self.kdesc['wg_size2'])
        
        if self.kdesc['wg_size'] < 1 or self.kdesc['wg_size'] > 512:
            raise AssertionError()
        self.kdesc['nWF_WG'] = math.ceil(self.kdesc['wg_size'] / 64)
  
    def download_kernel_descriptor(self):
        """Download the kernel settings into the LRAM (Link RAM)
        
        A kernel descriptor consists of a total of 32 32bit entirs:
        * The first 16 ones are for general settings, e.g. size of index space
        * The last 16 ones are for kernel parameter values
        
        Parameters
        ----------
        None
        
        Returns
        -------
        None
        """
        # clear descriptor
        for offset in range(0, 31):
            self.mmio.write(offset*4, 0)
        #set number of wavefronts in a WG
        #set the start address of the first instruction to be executed
        self.mmio.write(0, ((self.kdesc['nWF_WG']-1)<<28 | self.kdesc['start_addr']))
        self.mmio.write(1*4, self.kdesc['size0'])
        self.mmio.write(2*4, self.kdesc['size1'])
        self.mmio.write(3*4, self.kdesc['size2'])
        self.mmio.write(4*4, self.kdesc['offset0'])
        self.mmio.write(5*4, self.kdesc['offset1'])
        self.mmio.write(6*4, self.kdesc['offset2'])
        self.mmio.write(7*4, ((self.kdesc['nDim']-1)<<30 |
                              self.kdesc['wg_size2']<<20 |
                              self.kdesc['wg_size1']<<10 |
                              self.kdesc['wg_size0']))
        self.mmio.write(8*4, self.kdesc['n_wg0']-1)
        self.mmio.write(9*4, self.kdesc['n_wg1']-1)
        self.mmio.write(10*4, self.kdesc['n_wg2']-1)
        self.mmio.write(11*4, (self.kdesc['nParams']<<28 | self.kdesc['wg_size']))

        if len(self.params) == 0:
            raise AssertionError()
        
        for i in range(0, len(self.params)):
            self.mmio.write((16+i)*4, self.params[i])

    def execute_kernel(self):
        """Execute a kernel and wait until execution ends
        
        Parameters
        ----------
        None
        
        Returns
        -------
        float
            Execution time in seconds
        """
        start = time.time()
        self.mmio.write(self.start_reg_offset, 0x1)
        while self.mmio.read(self.status_reg_offset) == 0:
            pass
        end = time.time()
        return (end-start)

    def set_paramerter(self, paramIndex, val, mem_obj):
        """Set the value of a kernel parameter 
        
        Kernel parametrs are defind in the kernel header.
        
        Examples
        --------
            __kernel void foo(__global unsigned *in, __global unsigned *out, unsigned len)
                                paramIndex = 0         paramIndex=1           paramIndex=2
        Parameters
        ----------
        paramIndex : unsigned integer in the range [0..15]
            The index of the parameter to be set in kernel header. 
            
        
        val: void
            The Value that the parameter should take. It will be bitcasted to 32 unsigned int.
            
        mem_obj: xlnk
            An object fo class xlnk defined by Xilinx.
            This xlnk class enbales CMA (Contiguous Memory Allocator) memry management.
            It is needed to get the physical address of the parameter/memory buffer passed from iPython.
            
        Returns
        -------
        None
        """
        if paramIndex not in range(0,16):
            raise AssertionError()
        self.params[paramIndex] = mem_obj.cma_get_phy_addr(mem_obj.cma_cast(val, "void"))
        self.kdesc['nParams'] = len(self.params)

    def set_size(self, size, dim=0):
        """Set the size of the index space in any dimension.
        
        The size is equal to the number of work-items that will be launched 
        in the corresponding dimension.
        
        Parameters
        ----------
        size: unsigned int
            The required size for the index space in some dimension
        
        dim: unisgned int in range[0..2]
            The dimension whose size has to be set
            
        Returns
        -------
        None
        """
        if size < 1:
            raise AssertionError()
        if dim == 0:
            self.kdesc['size0'] = size
        elif dim == 1:
            self.kdesc['size1'] = size
        elif dim == 2:
            self.kdesc['size2'] = size
        else:
            raise AssertionError()
    
    def set_work_group_size(self, wg_size, dim=0):
        """Set the size of work-groups in any dimension
        
        Note
        Parameters
        ----------
        wg_size: unsigned int in range [1..512]
            The size of a work-group in some dimension
            
        dim: unsigned int in range [0..2]
            The dimension whose work-group size hast to be set
        
        Returns
        -------
        None
        """
        if wg_size < 1 | wg_size > 512:
            raise AssertionError()
        if dim == 0:
            self.kdesc['wg_size0'] = wg_size
        elif dim == 1:
            self.kdesc['wg_size1'] = wg_size
        elif dim == 2:
            self.kdesc['wg_size2'] = wg_size
        else:
            raise AssertionError()
    
    def set_num_dimensions(self, dims):
        """Set the number of dimesions of the required index space
        
        Note
        ----
        Only 1 & 2 dimensional index spaces are supported
        
        Parameters
        ----------
        dims: unsigned int in range [1..3]
            
        Returns
        -------
        None
        """
        if dims == 1 or dims == 2: #dim=3 is not yet supported
            self.kdesc['nDim'] = dims
        else:
            raise AssertionError()
            
    def set_offset(self, value=0, dim=0):
        """Sets the offsets of the index space in any dimension
        
        Examples
        --------
            Considering a kernel in a single dimension where 
            * offset = 10
            * size = 30
            30 kernels will be launched whose indices are in the range [10,40]
            
        Parameters
        ----------
        value: unsigned int
            Minimum number of the global id of any work-item
        
        Returns
        -------
        None
        """
        if value < 0:
            raise AssertionError()
        if dim == 0:
            self.kdesc['offset0'] = value
        elif dim == 1:
            self.kdesc['offset1'] = value
        elif dim == 2:
            self.kdesc['offset2'] = value
        else:
            raise AssertionError()
            
    def set_kernel_file(self, fileName):
        """Set the name of the file that contains the kernel OpenCL code
        
        Parameters
        ----------
        fileName: string
        
        Returns
        -------
        None
        """
        if os.path.isfile(fileName):
            self.kernelFileName = os.path.abspath(fileName)
        else:
            raise AssertionError()
            
    def compile_kernel(self, show_objdump=False):
        """Compile the kernel OpenCL code and read the generated binary
        
        The compilation process consists of three steps executed by the script "compile.sh"
        1. Clang compiles the OpenCL code into LLVM IR assembly (code.ll)
        2. The FGPU backend translates the IR into FGPU ISA and generates the object file (code.bin)
        3. The .text section of the generated object file is converted to an integer array (code.array)
        
        The content of the file code.array is read afterwards and stored in the variable kernel_code
        
        Note
        ----
        * Any mistakes in the OpenCL code will be shown if the clang-compilation was not successful
        
        Parameters
        ----------
        showObjdump: boolean
            Print an objdump for the .text section of the generated object file if set to true
        
        Returns
        -------
        None
        """
        if not os.path.isfile(self.kernelFileName):
            raise AssertionError()
        # The OpenCL kernel is compiled with clang into LLVM IR.
        # The result is written in the file code.ll
        p= Popen([general_const.COMPILE_SH, self.kernelFileName], stdout=PIPE, stderr=PIPE)
        out = p.communicate() # returns a tuble of byte arrays from stdout and stderr
        print(str(out[0], "utf-8")) # convert to string and print
        print(str(out[1], "utf-8")) # convert to string and print
        if p.returncode != 0:
            # if clang failed; print the log file and return
            with open(general_const.CLANG_LOG, 'r') as fin: 
                print (fin.read())
            return None
        #if clang compilation was not successful; the file code.ll will not exist
        if os.path.isfile(general_const.CODE_BIN):
            # Show the assembly of the compiled kernel
            if show_objdump:
                p= Popen([general_const.LLVM_OBJDUMP, "-d", general_const.CODE_BIN], stdout=PIPE)
                out = p.communicate() # returns a tuble of byte arrays from stdout and stderr
                print(str(out[0], "utf-8")) # convert to string and print
            with open(general_const.CODE_ARRAY) as f:
                self.kernel_code = []
                for line in f: # read rest of lines
                    record = [int(x,16) for x in line.split()]
                    self.kernel_code.append(record[0])
    
    def download_kernel(self):
        """Compute and download the kernel settings to FGPU
        
        Parameters
        ----------
        None
        
        Returns
        -------
        None
        """
        self.download_kernel_code()
        self.prepare_kernel_descriptor()
        self.download_kernel_descriptor()
        self.mmio.write(self.initiate_reg_offset, 0xFFFF)
        self.mmio.write(self.clean_cache_reg_offset, 0xFFFF)
        
    def set_bitFile(self, fileName):
        """Set the name of the bitstream file 
        
        Parameters
        ----------
        fileName: string
            
        Returns
        -------
        None
        """
        if os.path.isfile(fileName):
            self.bitfile = fileName
        else:
            raise AssertionError()

